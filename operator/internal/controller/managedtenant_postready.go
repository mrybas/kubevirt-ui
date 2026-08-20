/*
Copyright 2026.

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
*/

package controller

import (
	"context"
	"fmt"
	"strings"

	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/client-go/tools/clientcmd"
	"sigs.k8s.io/controller-runtime/pkg/client"

	platformv1alpha1 "github.com/mrybas/kubevirt-ui/operator/api/v1alpha1"
	"github.com/mrybas/kubevirt-ui/operator/internal/kube"
)

// TenantClientFor opens a client to a tenant's own API server. Replaced in
// tests, where there is no second cluster to talk to.
type TenantClientFor func(ctx context.Context, kubeconfig []byte) (client.Client, error)

// reconcileInsideTheTenant places what the tenant's own cluster needs and its
// control plane cannot be asked for until it is up.
//
// Everything here runs against a *different* API server, reached with the
// tenant's own admin credential, and only once that credential exists — which
// is why it is a phase of its own rather than part of the create path. The
// product does the same from a timer, for the same reason.
func (r *ManagedTenantReconciler) reconcileInsideTheTenant(
	ctx context.Context, obj *platformv1alpha1.ManagedTenant, namespace string,
) (ready bool, reason, message string, err error) {
	if obj.Spec.Workers.OS != "talos" {
		// A cloud-init worker gets its bootstrap credential from kubeadm, which
		// Kamaji already arranges.
		return true, "", "", nil
	}

	secrets := &corev1.Secret{}
	if err := r.Get(ctx, types.NamespacedName{
		Namespace: namespace, Name: obj.Name + "-talos-secrets",
	}, secrets); err != nil {
		if apierrors.IsNotFound(err) {
			return false, "WaitingForSecrets", "waiting for the machine secrets", nil
		}
		return false, "", "", fmt.Errorf("reading the machine secrets: %w", err)
	}
	token := string(secrets.Data["machine.token"])
	id, secret, found := strings.Cut(token, ".")
	if !found || id == "" || secret == "" {
		// Not something a retry fixes.
		return false, "MalformedToken",
			"the machine token is not in id.secret form, so no kubelet " +
				"credential can be derived from it", nil
	}

	tenantClient, reason, message, err := r.clientForTenant(ctx, obj, namespace)
	if err != nil {
		return false, "", "", err
	}
	if tenantClient == nil {
		return false, reason, message, nil
	}

	// Talos hands a worker one token for two jobs: trustd authenticates the
	// machine with it, and the kubelet uses the same value as a kubeadm-format
	// bootstrap credential. The first works as soon as the signer is up; the
	// second needs this Secret in the tenant's own kube-system, and Kamaji
	// creates the RBAC around it but not the Secret itself.
	//
	// Without it the node is a ghost: the signer issues its certificate, apid
	// and the kubelet both report healthy, and the cluster has no node at all,
	// because the kubelet's TLS bootstrap has nothing to authenticate with and
	// never files a CSR.
	name := "bootstrap-token-" + id
	existing := &corev1.Secret{}
	err = tenantClient.Get(ctx, types.NamespacedName{
		Namespace: "kube-system", Name: name,
	}, existing)
	if err == nil {
		return true, "Placed", fmt.Sprintf(
			"the kubelet bootstrap credential %s is in the tenant", name), nil
	}
	if !apierrors.IsNotFound(err) {
		return false, "TenantUnreachable", fmt.Sprintf(
			"the tenant's API did not answer: %v", err), nil
	}

	placed := &corev1.Secret{ObjectMeta: metav1.ObjectMeta{
		Namespace: "kube-system", Name: name,
		Labels: map[string]string{"kubevirt-ui.io/managed": "true"},
	}}
	placed.Type = corev1.SecretType("bootstrap.kubernetes.io/token")
	placed.StringData = map[string]string{
		"token-id":                       id,
		"token-secret":                   secret,
		"usage-bootstrap-authentication": "true",
		"usage-bootstrap-signing":        "true",
		"auth-extra-groups":              "system:bootstrappers:kubeadm:default-node-token",
	}
	if err := tenantClient.Create(ctx, placed); err != nil {
		if apierrors.IsAlreadyExists(err) {
			return true, "Placed", "the kubelet bootstrap credential is in the tenant", nil
		}
		return false, "TenantUnreachable", fmt.Sprintf(
			"could not place the kubelet bootstrap credential: %v", err), nil
	}
	kube.CountWrite(r.Scheme, placed, tenantControllerName, "created")
	return true, "Placed", fmt.Sprintf(
		"the kubelet bootstrap credential %s is in the tenant", name), nil
}

// clientForTenant opens a client to the tenant's own API server.
//
// By the **in-cluster** address: the admin secret carries several kubeconfigs
// and the external ones name an ingress host this process has no reason to be
// able to resolve or route to.
func (r *ManagedTenantReconciler) clientForTenant(
	ctx context.Context, obj *platformv1alpha1.ManagedTenant, namespace string,
) (client.Client, string, string, error) {
	secret := &corev1.Secret{}
	err := r.Get(ctx, types.NamespacedName{
		Namespace: namespace, Name: obj.Name + "-admin-kubeconfig",
	}, secret)
	if apierrors.IsNotFound(err) {
		return nil, "WaitingForKubeconfig", fmt.Sprintf(
			"waiting for Kamaji to mint %s/%s-admin-kubeconfig", namespace, obj.Name), nil
	}
	if err != nil {
		return nil, "", "", fmt.Errorf("reading the admin kubeconfig: %w", err)
	}

	raw := secret.Data["super-admin.svc"]
	if len(raw) == 0 {
		raw = secret.Data["admin.svc"]
	}
	if len(raw) == 0 {
		return nil, "WaitingForKubeconfig",
			"the admin secret carries no in-cluster kubeconfig yet", nil
	}

	open := r.TenantClient
	if open == nil {
		open = defaultTenantClient
	}
	tenantClient, err := open(ctx, raw)
	if err != nil {
		// Not an error of ours: a control plane that is not answering yet looks
		// exactly like this, and it fixes itself.
		return nil, "TenantUnreachable", fmt.Sprintf(
			"the tenant's API is not answering yet: %v", err), nil
	}
	return tenantClient, "", "", nil
}

func defaultTenantClient(_ context.Context, kubeconfig []byte) (client.Client, error) {
	config, err := clientcmd.RESTConfigFromKubeConfig(kubeconfig)
	if err != nil {
		return nil, fmt.Errorf("the kubeconfig is not readable: %w", err)
	}
	return client.New(config, client.Options{})
}

func insideCondition(ready bool, reason, message string) metav1.Condition {
	status := metav1.ConditionFalse
	if ready {
		status = metav1.ConditionTrue
	}
	if reason == "" {
		reason = "Waiting"
	}
	return metav1.Condition{
		Type:    platformv1alpha1.ConditionTenantBootstrapped,
		Status:  status,
		Reason:  reason,
		Message: message,
	}
}
