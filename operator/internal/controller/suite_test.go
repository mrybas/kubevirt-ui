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
	"os"
	"path/filepath"
	"testing"
	"time"

	corev1 "k8s.io/api/core/v1"
	rbacv1 "k8s.io/api/rbac/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	utilruntime "k8s.io/apimachinery/pkg/util/runtime"
	clientgoscheme "k8s.io/client-go/kubernetes/scheme"
	"k8s.io/client-go/rest"
	clonev1beta1 "kubevirt.io/api/clone/v1beta1"
	kubevirtv1 "kubevirt.io/api/core/v1"
	snapshotv1beta1 "kubevirt.io/api/snapshot/v1beta1"
	cdiv1 "kubevirt.io/containerized-data-importer-api/pkg/apis/core/v1beta1"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/envtest"
	logzap "sigs.k8s.io/controller-runtime/pkg/log/zap"
	metricsserver "sigs.k8s.io/controller-runtime/pkg/metrics/server"
	"sigs.k8s.io/yaml"

	platformv1alpha1 "github.com/mrybas/kubevirt-ui/operator/api/v1alpha1"
)

// The tests run against a real API server with a real manager, and assert
// through the manager's client. Two reasons, both learned the expensive way:
// a test that calls a helper proves the helper, not the controller; and a
// status read once proves nothing, because status is eventually consistent.
var (
	testEnv   *envtest.Environment
	cfg       *rest.Config
	k8sClient client.Client
	// k8sReader goes straight to the API server. For assertions about a thing
	// NOT existing, and for reads of something just written: the cached client
	// answers from an informer that may not have caught up, which makes
	// "absent" and "not yet delivered" the same answer — and the dangerous
	// direction of that is a safety test passing because it looked too early.
	k8sReader client.Reader
	testCtx   context.Context
	stopMgr   context.CancelFunc
)

var networkReconciler *ManagedNetworkReconciler

func TestMain(m *testing.M) {
	code, err := runSuite(m)
	if err != nil {
		fmt.Fprintf(os.Stderr, "test suite setup failed: %v\n", err)
		os.Exit(1)
	}
	os.Exit(code)
}

// managerUser is who the controllers are, as far as the API server is
// concerned. Impersonated rather than a real ServiceAccount because envtest has
// no token issuer worth the trouble; the authorisation path is identical.
const managerUser = "system:serviceaccount:kubevirt-ui-operator-system:kubevirt-ui-operator-controller-manager"

// restrictedConfig binds the generated ClusterRole to that user and returns a
// config that impersonates it.
func restrictedConfig(admin *rest.Config) (*rest.Config, error) {
	adminClient, err := client.New(admin, client.Options{})
	if err != nil {
		return nil, fmt.Errorf("admin client: %w", err)
	}

	raw, err := os.ReadFile(filepath.Join("..", "..", "config", "rbac", "role.yaml"))
	if err != nil {
		return nil, fmt.Errorf("reading the operator's role: %w", err)
	}
	role := &rbacv1.ClusterRole{}
	if err := yaml.Unmarshal(raw, role); err != nil {
		return nil, fmt.Errorf("parsing the operator's role: %w", err)
	}
	role.Name = "kubevirt-ui-operator-manager-role"
	role.ResourceVersion = ""
	if err := adminClient.Create(context.Background(), role); err != nil {
		return nil, fmt.Errorf("installing the operator's role: %w", err)
	}

	binding := &rbacv1.ClusterRoleBinding{
		ObjectMeta: metav1.ObjectMeta{Name: "kubevirt-ui-operator-manager-binding"},
		RoleRef: rbacv1.RoleRef{
			APIGroup: rbacv1.GroupName, Kind: "ClusterRole", Name: role.Name,
		},
		Subjects: []rbacv1.Subject{{
			APIGroup: rbacv1.GroupName, Kind: "User", Name: managerUser,
		}},
	}
	if err := adminClient.Create(context.Background(), binding); err != nil {
		return nil, fmt.Errorf("binding the operator's role: %w", err)
	}

	restricted := rest.CopyConfig(admin)
	restricted.Impersonate = rest.ImpersonationConfig{UserName: managerUser}
	return restricted, nil
}

func runSuite(m *testing.M) (int, error) {
	ctrl.SetLogger(logzap.New(logzap.UseDevMode(true), logzap.WriteTo(os.Stderr)))

	scheme := runtime.NewScheme()
	utilruntime.Must(clientgoscheme.AddToScheme(scheme))
	utilruntime.Must(platformv1alpha1.AddToScheme(scheme))
	utilruntime.Must(cdiv1.AddToScheme(scheme))
	utilruntime.Must(kubevirtv1.AddToScheme(scheme))
	utilruntime.Must(snapshotv1beta1.AddToScheme(scheme))
	utilruntime.Must(clonev1beta1.AddToScheme(scheme))

	testEnv = &envtest.Environment{
		CRDDirectoryPaths: []string{
			filepath.Join("..", "..", "config", "crd", "bases"),
			// CDI's own schemas, exported from the live cluster. The controllers
			// that would act on them are absent on purpose: this suite tests what
			// we write, and fakes what CDI would answer.
			filepath.Join("..", "..", "test", "crds"),
		},
		ErrorIfCRDPathMissing: true,
	}

	var err error
	cfg, err = testEnv.Start()
	if err != nil {
		return 0, fmt.Errorf("starting envtest: %w", err)
	}
	defer func() { _ = testEnv.Stop() }()

	// The controllers run as the ServiceAccount the chart gives them, not as
	// the admin envtest hands out.
	//
	// Three live runs have now been spent on a verb the code needed and the
	// role did not grant — `datavolumes/source`, `secrets` create, a Role that
	// could not be created because the writer did not hold what it was granting
	// — and none of them could fail in a suite where every request is
	// cluster-admin. The RBAC is part of what this operator is; testing it
	// against a subject that ignores RBAC tests the other half only.
	restricted, err := restrictedConfig(cfg)
	if err != nil {
		return 0, err
	}

	mgr, err := ctrl.NewManager(restricted, ctrl.Options{
		Scheme:  scheme,
		Metrics: metricsserver.Options{BindAddress: "0"},
	})
	if err != nil {
		return 0, fmt.Errorf("creating manager: %w", err)
	}

	if err := (&ManagedImageReconciler{
		Client:   mgr.GetClient(),
		Scheme:   mgr.GetScheme(),
		Recorder: mgr.GetEventRecorderFor("managedimage"),
	}).SetupWithManager(mgr); err != nil {
		return 0, fmt.Errorf("wiring image controller: %w", err)
	}

	if err := (&AnnouncementPolicyReconciler{
		Client:   mgr.GetClient(),
		Scheme:   mgr.GetScheme(),
		Recorder: mgr.GetEventRecorderFor("announcementpolicy"),
	}).SetupWithManager(mgr); err != nil {
		return 0, fmt.Errorf("wiring announcement controller: %w", err)
	}

	networkReconciler = &ManagedNetworkReconciler{
		Client:   mgr.GetClient(),
		Scheme:   mgr.GetScheme(),
		Recorder: mgr.GetEventRecorderFor("managednetwork"),
		// Pinned, because the underlay tests plant kube-ovn CNI DaemonSets in
		// several namespaces of their own and discovery would pick whichever
		// the API server listed first.
		KubeOVNNamespace: "kube-ovn",
		TenantSupernet:   "10.200.0.0/14",
		MgmtCIDRs:        []string{"10.198.160.1/32", "10.198.160.2/32"},
		// The real one. envtest has no apiserver pod and no kubeadm ConfigMap,
		// which is the same shape as a managed control plane — so discovery
		// genuinely fails here, and that is worth exercising rather than
		// stubbing.
		APIReader: mgr.GetAPIReader(),
	}
	if err := networkReconciler.SetupWithManager(mgr); err != nil {
		return 0, fmt.Errorf("wiring network controller: %w", err)
	}

	if err := (&ManagedTenantReconciler{
		Client:    mgr.GetClient(),
		Scheme:    mgr.GetScheme(),
		Recorder:  mgr.GetEventRecorderFor("managedtenant"),
		APIReader: mgr.GetAPIReader(),
		// Pinned, like the network controller's: the tests plant kube-ovn's own
		// objects here, and discovery would pick whichever namespace the API
		// server listed first.
		KubeOVNNamespace: "kube-ovn",
	}).SetupWithManager(mgr); err != nil {
		return 0, fmt.Errorf("wiring tenant controller: %w", err)
	}

	if err := (&TalosBootstrapReconciler{
		Client:   mgr.GetClient(),
		Scheme:   mgr.GetScheme(),
		Recorder: mgr.GetEventRecorderFor("talosbootstrap"),
	}).SetupWithManager(mgr); err != nil {
		return 0, fmt.Errorf("wiring talos bootstrap controller: %w", err)
	}

	if err := (&ManagedNetworkPeeringReconciler{
		Client:   mgr.GetClient(),
		Scheme:   mgr.GetScheme(),
		Recorder: mgr.GetEventRecorderFor("managednetworkpeering"),
	}).SetupWithManager(mgr); err != nil {
		return 0, fmt.Errorf("wiring peering controller: %w", err)
	}

	if err := (&ManagedUnderlayReconciler{
		Client:   mgr.GetClient(),
		Scheme:   mgr.GetScheme(),
		Recorder: mgr.GetEventRecorderFor("managedunderlay"),
	}).SetupWithManager(mgr); err != nil {
		return 0, fmt.Errorf("wiring underlay controller: %w", err)
	}

	if err := (&ManagedVMOperationReconciler{
		Client:   mgr.GetClient(),
		Scheme:   mgr.GetScheme(),
		Recorder: mgr.GetEventRecorderFor("managedvmoperation"),
	}).SetupWithManager(mgr); err != nil {
		return 0, fmt.Errorf("wiring operation controller: %w", err)
	}

	if err := (&ManagedVMTemplateReconciler{
		Client:   mgr.GetClient(),
		Scheme:   mgr.GetScheme(),
		Recorder: mgr.GetEventRecorderFor("managedvmtemplate"),
	}).SetupWithManager(mgr); err != nil {
		return 0, fmt.Errorf("wiring template controller: %w", err)
	}

	if err := (&ManagedVMReconciler{
		Client:           mgr.GetClient(),
		Scheme:           mgr.GetScheme(),
		Recorder:         mgr.GetEventRecorderFor("managedvm"),
		KubeOVNNamespace: func() string { return "kube-ovn" },
	}).SetupWithManager(mgr); err != nil {
		return 0, fmt.Errorf("wiring vm controller: %w", err)
	}

	testCtx, stopMgr = context.WithCancel(context.Background())
	defer stopMgr()

	go func() {
		if err := mgr.Start(testCtx); err != nil {
			fmt.Fprintf(os.Stderr, "manager stopped: %v\n", err)
		}
	}()

	if !mgr.GetCache().WaitForCacheSync(testCtx) {
		return 0, fmt.Errorf("cache did not sync")
	}
	// The tests are the cluster: they play CDI finishing an import, MetalLB
	// handing out an address, kube-ovn allocating one. That is admin work and
	// it is not what is under test, so it keeps an admin client — while the
	// manager above runs as the ServiceAccount the chart gives it, which is.
	k8sClient, err = client.New(cfg, client.Options{Scheme: scheme})
	if err != nil {
		return 0, fmt.Errorf("admin client: %w", err)
	}
	k8sReader = k8sClient

	// The controllers read cluster-wide configuration from this namespace, so
	// it has to exist before any of them run.
	if err := k8sClient.Create(testCtx, &corev1.Namespace{
		ObjectMeta: metav1.ObjectMeta{Name: "kubevirt-ui-system"},
	}); err != nil {
		return 0, fmt.Errorf("creating the system namespace: %w", err)
	}

	return m.Run(), nil
}

// mustNamespace creates a namespace carrying the product's labels, the way the
// folder/environment machinery creates them.
func mustNamespace(t *testing.T, name, project string) {
	t.Helper()
	ns := &corev1.Namespace{
		ObjectMeta: metav1.ObjectMeta{
			Name: name,
			Labels: map[string]string{
				"kubevirt-ui.io/managed":     "true",
				"kubevirt-ui.io/environment": "dev",
			},
		},
	}
	if project != "" {
		ns.Labels["kubevirt-ui.io/project"] = project
	}
	if err := k8sClient.Create(testCtx, ns); err != nil {
		t.Fatalf("creating namespace %s: %v", name, err)
	}
}

// eventually retries cond until it holds or the deadline passes, and reports
// the last failure reason so a red test says what it was waiting for.
func eventually(t *testing.T, what string, cond func() error) {
	t.Helper()
	deadline := time.Now().Add(20 * time.Second)
	var last error
	for time.Now().Before(deadline) {
		last = cond()
		if last == nil {
			return
		}
		time.Sleep(100 * time.Millisecond)
	}
	t.Fatalf("timed out waiting for %s: %v", what, last)
}

// consistently asserts cond keeps holding for the whole window. Used where the
// interesting claim is that nothing happened.
func consistently(t *testing.T, what string, window time.Duration, cond func() error) {
	t.Helper()
	deadline := time.Now().Add(window)
	for time.Now().Before(deadline) {
		if err := cond(); err != nil {
			t.Fatalf("%s stopped holding: %v", what, err)
		}
		time.Sleep(100 * time.Millisecond)
	}
}
