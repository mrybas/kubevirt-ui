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
	"bytes"
	"context"
	"fmt"
	"strings"
	"time"

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
	ctx context.Context, obj *platformv1alpha1.ManagedTenant, namespace, vip string,
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

	// Bounded, and bounded here rather than trusted to the callee: everything
	// below talks to somebody else's API server, and one tenant whose control
	// plane accepts connections and never answers would otherwise hold this
	// controller's pass — and with it every other tenant's — for as long as it
	// stays that way.
	ctx, cancel := context.WithTimeout(ctx, insideTenantTimeout)
	defer cancel()

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
	if err := r.replicateCSICredential(ctx, obj, namespace, vip, tenantClient); err != nil {
		return false, "TenantUnreachable", fmt.Sprintf(
			"could not put the storage credential in the tenant: %v", err), nil
	}

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
			"the tenant's API did not answer within %s: %v", insideTenantTimeout, err), nil
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

// replicateCSICredential copies the host credential the tenant's storage driver
// reads into the tenant's own cluster.
//
// The opposite discipline to the bootstrap token beside it, and for a reason
// worth naming: that one is *the* credential and is written once, because
// rotating it invalidates what every worker holds. This one is a **copy** of a
// credential that lives somewhere else, so it is kept in step — a stale copy is
// a driver that cannot reach the host API, and every volume it is asked for
// fails with an authentication error that names nothing about a secret.
//
// Absent on the host side is not an error here: a tenant without storage never
// has one, and the storage path creates it when there is storage to wire.
func (r *ManagedTenantReconciler) replicateCSICredential(
	ctx context.Context, obj *platformv1alpha1.ManagedTenant,
	namespace, vip string, tenantClient client.Client,
) error {
	source := &corev1.Secret{}
	if err := r.Get(ctx, types.NamespacedName{
		Namespace: namespace, Name: csiCredentialSecret,
	}, source); err != nil {
		if apierrors.IsNotFound(err) {
			return nil
		}
		return fmt.Errorf("reading %s/%s: %w", namespace, csiCredentialSecret, err)
	}
	payload := source.Data["kubeconfig"]
	if len(payload) == 0 {
		return nil
	}
	// The copy inside the tenant reaches the host apiserver over the transit
	// plane, not through the border — see managedtenant_hostapi.go. The host
	// secret keeps its own address.
	payload, err := throughTheTransitPlane(payload, vip)
	if err != nil {
		return err
	}

	copied := &corev1.Secret{}
	err = tenantClient.Get(ctx, types.NamespacedName{
		Namespace: "kube-system", Name: csiCredentialSecret,
	}, copied)
	switch {
	case apierrors.IsNotFound(err):
		placed := &corev1.Secret{ObjectMeta: metav1.ObjectMeta{
			Namespace: "kube-system", Name: csiCredentialSecret,
			Labels: map[string]string{"kubevirt-ui.io/managed": "true"},
		}}
		placed.Type = corev1.SecretTypeOpaque
		placed.Data = map[string][]byte{"kubeconfig": payload}
		if err := tenantClient.Create(ctx, placed); err != nil &&
			!apierrors.IsAlreadyExists(err) {
			return err
		}
		kube.CountWrite(r.Scheme, placed, tenantControllerName, "created")
		return nil
	case err != nil:
		return err
	}

	if bytes.Equal(copied.Data["kubeconfig"], payload) {
		return nil
	}
	copied.Data = map[string][]byte{"kubeconfig": payload}
	if err := tenantClient.Update(ctx, copied); err != nil {
		return err
	}
	kube.CountWrite(r.Scheme, copied, tenantControllerName, "updated")
	return nil
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

// insideTenantTimeout is how long the tenant's own API server gets to answer.
//
// Short on purpose. Nothing here is urgent — the credential is placed once and
// the pass comes back every ten seconds — while the cost of no bound is a
// controller that stops reconciling every other tenant because one of them is
// accepting connections and not replying.
const insideTenantTimeout = 10 * time.Second

// csiCredentialSecret is the host credential the tenant's storage driver reads,
// by the name it reads it under on both sides.
const csiCredentialSecret = "infra-cluster-credentials"

func defaultTenantClient(_ context.Context, kubeconfig []byte) (client.Client, error) {
	config, err := clientcmd.RESTConfigFromKubeConfig(kubeconfig)
	if err != nil {
		return nil, fmt.Errorf("the kubeconfig is not readable: %w", err)
	}
	// The context bounds each call; this bounds the client itself, because a
	// request that never gets a response header is not covered by anything the
	// caller passes.
	config.Timeout = insideTenantTimeout
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
