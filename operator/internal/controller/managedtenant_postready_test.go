package controller

import (
	"context"
	"fmt"
	"testing"
	"time"

	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"

	platformv1alpha1 "github.com/mrybas/kubevirt-ui/operator/api/v1alpha1"
)

// tenantCluster stands in for the tenant's own API server. There is no second
// cluster in envtest, and standing one up would test controller-runtime rather
// than this: what is under test is which object is placed, where, and when.
type tenantCluster struct {
	client  client.Client
	opened  int
	failing bool
}

func (c *tenantCluster) open(context.Context, []byte) (client.Client, error) {
	c.opened++
	if c.failing {
		return nil, fmt.Errorf("connection refused")
	}
	return c.client, nil
}

func newTenantCluster(t *testing.T) *tenantCluster {
	t.Helper()
	return &tenantCluster{client: fake.NewClientBuilder().
		WithScheme(k8sClient.Scheme()).Build()}
}

func mustAdminKubeconfig(t *testing.T, namespace, tenant, key string) {
	t.Helper()
	secret := &corev1.Secret{ObjectMeta: metav1.ObjectMeta{
		Namespace: namespace, Name: tenant + "-admin-kubeconfig",
	}}
	secret.Data = map[string][]byte{key: []byte("apiVersion: v1\nkind: Config\n")}
	if err := k8sClient.Create(testCtx, secret); err != nil && !apierrors.IsAlreadyExists(err) {
		t.Fatalf("minting the admin kubeconfig: %v", err)
	}
}

func mustMachineSecrets(t *testing.T, namespace, tenant, token string) {
	t.Helper()
	secret := &corev1.Secret{ObjectMeta: metav1.ObjectMeta{
		Namespace: namespace, Name: tenant + "-talos-secrets",
	}}
	secret.Data = map[string][]byte{"machine.token": []byte(token)}
	if err := k8sClient.Create(testCtx, secret); err != nil && !apierrors.IsAlreadyExists(err) {
		t.Fatalf("planting the machine secrets: %v", err)
	}
}

// TestTheKubeletCredentialIsPlacedInsideTheTenant.
//
// Talos hands a worker one token for two jobs, and only the first works on its
// own: trustd authenticates the machine, and the kubelet needs the same value
// as a kubeadm bootstrap credential in the tenant's own kube-system. Kamaji
// creates the RBAC around it and not the Secret.
//
// Its absence names nothing. The certificate is issued, apid and the kubelet
// report healthy, and the cluster has no node — because the kubelet's TLS
// bootstrap has nothing to authenticate with and never files a CSR.
func TestTheKubeletCredentialIsPlacedInsideTheTenant(t *testing.T) {
	mustNamespace(t, "tenant-tin1", "")
	mustMachineSecrets(t, "tenant-tin1", "tin1", "abcdef.0123456789abcdef")
	mustAdminKubeconfig(t, "tenant-tin1", "tin1", "super-admin.svc")

	cluster := newTenantCluster(t)
	reconciler := &ManagedTenantReconciler{
		Client: k8sClient, Scheme: k8sClient.Scheme(), APIReader: k8sReader,
		TenantClient: cluster.open,
	}
	ready, reason, message, err := reconciler.reconcileInsideTheTenant(
		testCtx, talosTenant("tin1"), "tenant-tin1")
	if err != nil {
		t.Fatalf("reconcileInsideTheTenant: %v", err)
	}
	if !ready {
		t.Fatalf("not placed: %s %s", reason, message)
	}

	placed := &corev1.Secret{}
	if err := cluster.client.Get(testCtx, types.NamespacedName{
		Namespace: "kube-system", Name: "bootstrap-token-abcdef",
	}, placed); err != nil {
		t.Fatalf("nothing was placed in the tenant: %v", err)
	}
	if placed.Type != "bootstrap.kubernetes.io/token" {
		t.Errorf("type = %q — the kubelet's bootstrap flow only reads that one",
			placed.Type)
	}
	if got := placed.StringData["token-secret"]; got != "0123456789abcdef" {
		t.Errorf("token-secret = %q", got)
	}
	if got := placed.StringData["auth-extra-groups"]; got != "system:bootstrappers:kubeadm:default-node-token" {
		t.Errorf("auth-extra-groups = %q — without it the CSR is filed by "+
			"somebody the approvers do not recognise", got)
	}

	// Twice is once: the token never rotates, and rewriting it would invalidate
	// the credential every worker already holds.
	before := placed.ResourceVersion
	if _, _, _, err := reconciler.reconcileInsideTheTenant(
		testCtx, talosTenant("tin1"), "tenant-tin1"); err != nil {
		t.Fatalf("second pass: %v", err)
	}
	after := &corev1.Secret{}
	if err := cluster.client.Get(testCtx, types.NamespacedName{
		Namespace: "kube-system", Name: "bootstrap-token-abcdef",
	}, after); err != nil {
		t.Fatalf("reading it back: %v", err)
	}
	if after.ResourceVersion != before {
		t.Errorf("it rewrote the credential every worker already holds")
	}
}

// TestAColdControlPlaneIsWaitedForRatherThanFailed. A tenant whose API is not
// answering yet looks exactly like this, and it fixes itself.
func TestAColdControlPlaneIsWaitedForRatherThanFailed(t *testing.T) {
	mustNamespace(t, "tenant-tin2", "")
	mustMachineSecrets(t, "tenant-tin2", "tin2", "abcdef.0123456789abcdef")
	mustAdminKubeconfig(t, "tenant-tin2", "tin2", "super-admin.svc")

	cluster := newTenantCluster(t)
	cluster.failing = true
	reconciler := &ManagedTenantReconciler{
		Client: k8sClient, Scheme: k8sClient.Scheme(), APIReader: k8sReader,
		TenantClient: cluster.open,
	}
	ready, reason, _, err := reconciler.reconcileInsideTheTenant(
		testCtx, talosTenant("tin2"), "tenant-tin2")
	if err != nil {
		t.Fatalf("a cold control plane was reported as our error: %v", err)
	}
	if ready || reason != "TenantUnreachable" {
		t.Errorf("ready=%v reason=%q", ready, reason)
	}
}

// TestAMalformedTokenIsRefusedRatherThanRetried. Nothing about it improves by
// coming back.
func TestAMalformedTokenIsRefusedRatherThanRetried(t *testing.T) {
	mustNamespace(t, "tenant-tin3", "")
	mustMachineSecrets(t, "tenant-tin3", "tin3", "no-dot-here")
	mustAdminKubeconfig(t, "tenant-tin3", "tin3", "super-admin.svc")

	cluster := newTenantCluster(t)
	reconciler := &ManagedTenantReconciler{
		Client: k8sClient, Scheme: k8sClient.Scheme(), APIReader: k8sReader,
		TenantClient: cluster.open,
	}
	ready, reason, _, err := reconciler.reconcileInsideTheTenant(
		testCtx, talosTenant("tin3"), "tenant-tin3")
	if err != nil {
		t.Fatalf("reconcileInsideTheTenant: %v", err)
	}
	if ready || reason != "MalformedToken" {
		t.Errorf("ready=%v reason=%q", ready, reason)
	}
	if cluster.opened != 0 {
		t.Error("it dialled the tenant to place something it could not derive")
	}
}

// TestACloudInitTenantNeedsNothingPlaced. Its kubelet gets its credential from
// kubeadm, which Kamaji already arranges.
func TestACloudInitTenantNeedsNothingPlaced(t *testing.T) {
	cluster := newTenantCluster(t)
	reconciler := &ManagedTenantReconciler{
		Client: k8sClient, Scheme: k8sClient.Scheme(), TenantClient: cluster.open,
	}
	ready, _, _, err := reconciler.reconcileInsideTheTenant(
		testCtx, plainTenant("tin4"), "tenant-tin4")
	if err != nil || !ready {
		t.Fatalf("ready=%v err=%v", ready, err)
	}
	if cluster.opened != 0 {
		t.Error("it dialled a tenant that needs nothing")
	}
	_ = platformv1alpha1.ConditionTenantBootstrapped
}

// blockingClient is a tenant API that accepts the call and never answers —
// the shape that has no timeout of its own and is not a network error either.
type blockingClient struct{ client.Client }

func (b blockingClient) Get(ctx context.Context, _ client.ObjectKey, _ client.Object,
	_ ...client.GetOption) error {
	<-ctx.Done()
	return ctx.Err()
}

// TestOneSilentTenantDoesNotHoldTheRest.
//
// Everything in this phase talks to somebody else's API server. A control plane
// that accepts connections and never replies is not an error and not a refusal;
// without a bound it is a controller that stops reconciling every other tenant
// for as long as that one stays that way.
func TestOneSilentTenantDoesNotHoldTheRest(t *testing.T) {
	mustNamespace(t, "tenant-tin5", "")
	mustMachineSecrets(t, "tenant-tin5", "tin5", "abcdef.0123456789abcdef")
	mustAdminKubeconfig(t, "tenant-tin5", "tin5", "super-admin.svc")

	silent := &tenantCluster{client: blockingClient{
		Client: fake.NewClientBuilder().WithScheme(k8sClient.Scheme()).Build(),
	}}
	reconciler := &ManagedTenantReconciler{
		Client: k8sClient, Scheme: k8sClient.Scheme(), APIReader: k8sReader,
		TenantClient: silent.open,
	}

	start := time.Now()
	ready, reason, message, err := reconciler.reconcileInsideTheTenant(
		testCtx, talosTenant("tin5"), "tenant-tin5")
	waited := time.Since(start)

	if err != nil {
		t.Fatalf("a silent tenant was reported as our error: %v", err)
	}
	if ready {
		t.Fatal("it claimed the credential was placed")
	}
	if reason != "TenantUnreachable" {
		t.Errorf("reason = %q (%s)", reason, message)
	}
	if waited > insideTenantTimeout+5*time.Second {
		t.Errorf("it waited %s — the bound is %s, and the whole point is that "+
			"one tenant cannot hold the pass", waited, insideTenantTimeout)
	}
}

// TestTheStorageCredentialIsKeptInStepRatherThanWrittenOnce.
//
// The opposite discipline to the bootstrap token beside it, and deliberately.
// That one is *the* credential and is written once, because rotating it
// invalidates what every worker holds. This one is a copy of a credential that
// lives on the host, so a stale copy is a driver that cannot reach the host API
// — and every volume it is asked for fails with an authentication error that
// says nothing about a secret.
func TestTheStorageCredentialIsKeptInStepRatherThanWrittenOnce(t *testing.T) {
	mustNamespace(t, "tenant-tin6", "")
	mustMachineSecrets(t, "tenant-tin6", "tin6", "abcdef.0123456789abcdef")
	mustAdminKubeconfig(t, "tenant-tin6", "tin6", "super-admin.svc")

	host := &corev1.Secret{ObjectMeta: metav1.ObjectMeta{
		Namespace: "tenant-tin6", Name: "infra-cluster-credentials",
	}}
	host.Data = map[string][]byte{"kubeconfig": []byte("first")}
	if err := k8sClient.Create(testCtx, host); err != nil && !apierrors.IsAlreadyExists(err) {
		t.Fatalf("creating the host credential: %v", err)
	}

	cluster := newTenantCluster(t)
	reconciler := &ManagedTenantReconciler{
		Client: k8sClient, Scheme: k8sClient.Scheme(), APIReader: k8sReader,
		TenantClient: cluster.open,
	}
	if _, _, _, err := reconciler.reconcileInsideTheTenant(
		testCtx, talosTenant("tin6"), "tenant-tin6"); err != nil {
		t.Fatalf("reconcileInsideTheTenant: %v", err)
	}

	copied := &corev1.Secret{}
	if err := cluster.client.Get(testCtx, types.NamespacedName{
		Namespace: "kube-system", Name: "infra-cluster-credentials",
	}, copied); err != nil {
		t.Fatalf("the credential was not copied into the tenant: %v", err)
	}
	if string(copied.Data["kubeconfig"]) != "first" {
		t.Fatalf("copied %q", copied.Data["kubeconfig"])
	}

	// Rotated on the host: the copy follows.
	live := &corev1.Secret{}
	if err := k8sClient.Get(testCtx, types.NamespacedName{
		Namespace: "tenant-tin6", Name: "infra-cluster-credentials",
	}, live); err != nil {
		t.Fatalf("reading the host credential: %v", err)
	}
	live.Data = map[string][]byte{"kubeconfig": []byte("second")}
	if err := k8sClient.Update(testCtx, live); err != nil {
		t.Fatalf("rotating it: %v", err)
	}
	if _, _, _, err := reconciler.reconcileInsideTheTenant(
		testCtx, talosTenant("tin6"), "tenant-tin6"); err != nil {
		t.Fatalf("second pass: %v", err)
	}
	if err := cluster.client.Get(testCtx, types.NamespacedName{
		Namespace: "kube-system", Name: "infra-cluster-credentials",
	}, copied); err != nil {
		t.Fatalf("reading the copy: %v", err)
	}
	if string(copied.Data["kubeconfig"]) != "second" {
		t.Errorf("the copy is stale: %q", copied.Data["kubeconfig"])
	}

	// Unchanged is not rewritten: this runs every pass.
	before := copied.ResourceVersion
	if _, _, _, err := reconciler.reconcileInsideTheTenant(
		testCtx, talosTenant("tin6"), "tenant-tin6"); err != nil {
		t.Fatalf("third pass: %v", err)
	}
	if err := cluster.client.Get(testCtx, types.NamespacedName{
		Namespace: "kube-system", Name: "infra-cluster-credentials",
	}, copied); err != nil {
		t.Fatalf("reading the copy: %v", err)
	}
	if copied.ResourceVersion != before {
		t.Error("it rewrote a copy that had not changed")
	}
}

// TestATenantWithoutStorageIsNotGivenACredential. A tenant with no storage
// never has a host-side one, and its absence is not a failure to report.
func TestATenantWithoutStorageIsNotGivenACredential(t *testing.T) {
	mustNamespace(t, "tenant-tin7", "")
	mustMachineSecrets(t, "tenant-tin7", "tin7", "abcdef.0123456789abcdef")
	mustAdminKubeconfig(t, "tenant-tin7", "tin7", "super-admin.svc")

	cluster := newTenantCluster(t)
	reconciler := &ManagedTenantReconciler{
		Client: k8sClient, Scheme: k8sClient.Scheme(), APIReader: k8sReader,
		TenantClient: cluster.open,
	}
	ready, reason, message, err := reconciler.reconcileInsideTheTenant(
		testCtx, talosTenant("tin7"), "tenant-tin7")
	if err != nil || !ready {
		t.Fatalf("ready=%v reason=%s %s err=%v", ready, reason, message, err)
	}
	copied := &corev1.Secret{}
	err = cluster.client.Get(testCtx, types.NamespacedName{
		Namespace: "kube-system", Name: "infra-cluster-credentials",
	}, copied)
	if err == nil {
		t.Error("it invented a storage credential for a tenant with no storage")
	} else if !apierrors.IsNotFound(err) {
		t.Fatalf("reading the copy: %v", err)
	}
}
