package controller

import (
	"encoding/base64"
	"fmt"
	"strings"
	"testing"
	"time"

	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	"k8s.io/apimachinery/pkg/types"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/yaml"

	platformv1alpha1 "github.com/mrybas/kubevirt-ui/operator/api/v1alpha1"
)

// mustKubeOVNResolver plants the address kube-ovn states for VPC workloads.
//
// Every tenant in a VPC needs it before its worker config can be written, so
// the tests that build one plant it the way the cluster would.
func mustKubeOVNResolver(t *testing.T) {
	t.Helper()
	ns := &corev1.Namespace{ObjectMeta: metav1.ObjectMeta{Name: "kube-ovn"}}
	if err := k8sClient.Create(testCtx, ns); err != nil && !apierrors.IsAlreadyExists(err) {
		t.Fatalf("creating the kube-ovn namespace: %v", err)
	}
	config := &corev1.ConfigMap{ObjectMeta: metav1.ObjectMeta{
		Namespace: "kube-ovn", Name: "vpc-dns-config",
	}}
	config.Data = map[string]string{"coredns-vip": "10.96.0.200"}
	if err := k8sClient.Create(testCtx, config); err != nil && !apierrors.IsAlreadyExists(err) {
		t.Fatalf("planting the resolver: %v", err)
	}
}

// issueCA plays cert-manager and Kamaji, which envtest has nobody to play.
func issueCA(t *testing.T, namespace, name, key string) {
	t.Helper()
	secret := &corev1.Secret{ObjectMeta: metav1.ObjectMeta{
		Namespace: namespace, Name: name,
	}}
	secret.Data = map[string][]byte{key: []byte("a-certificate")}
	if err := k8sClient.Create(testCtx, secret); err != nil && !apierrors.IsAlreadyExists(err) {
		t.Fatalf("issuing %s/%s: %v", namespace, name, err)
	}
}

func workerConfig(t *testing.T, namespace, name string) map[string]any {
	t.Helper()
	template := &unstructured.Unstructured{}
	template.SetGroupVersionKind(talosConfigTemplateGVK)
	if err := k8sClient.Get(testCtx, types.NamespacedName{
		Namespace: namespace, Name: name,
	}, template); err != nil {
		t.Fatalf("reading the worker template: %v", err)
	}
	data, _, _ := unstructured.NestedString(template.Object,
		"spec", "template", "spec", "data")
	config := map[string]any{}
	if err := yaml.Unmarshal([]byte(data), &config); err != nil {
		t.Fatalf("the config is not YAML: %v", err)
	}
	return config
}

// TestTheWorkerConfigIsNotWrittenUntilBothCAsExist.
//
// The template is immutable: whatever is written is what every worker of this
// tenant gets for its whole life. Kamaji mints the Kubernetes CA while the
// config is being assembled, and the two used to race — measured to the second,
// a read that missed a secret created one second later, and a tenant that then
// failed every boot with "missing accepted Kubernetes CAs" twelve seconds in,
// before any network, so it read as a mystery rather than a race.
func TestTheWorkerConfigIsNotWrittenUntilBothCAsExist(t *testing.T) {
	mustKubeOVNResolver(t)
	mustTenant(t, vpcTalosTenant("twk1"))
	eventually(t, "the request for an address", func() error {
		_, err := cpService("twk1")
		return err
	})
	assignAddress(t, "twk1", "10.199.0.121")

	eventually(t, "the tenant to name the missing CA", func() error {
		condition := tenantCondition(getTenant(t, "twk1"),
			platformv1alpha1.ConditionWorkersReady)
		if condition == nil {
			return fmt.Errorf("no condition")
		}
		if !strings.HasPrefix(condition.Reason, "WaitingFor") {
			return fmt.Errorf("reason = %q (%s)", condition.Reason, condition.Message)
		}
		return nil
	})
	template := &unstructured.Unstructured{}
	template.SetGroupVersionKind(talosConfigTemplateGVK)
	err := k8sReader.Get(testCtx, types.NamespacedName{
		Namespace: "tenant-twk1", Name: "twk1-workers",
	}, template)
	if err == nil {
		t.Fatal("a worker config was written before both CAs existed, and it " +
			"cannot be rewritten")
	} else if !apierrors.IsNotFound(err) {
		t.Fatalf("reading the template: %v", err)
	}

	issueCA(t, "tenant-twk1", "twk1-talos-ca", "tls.crt")
	// Still not enough: the Kubernetes CA is a different certificate, and
	// without it Talos starts and never brings the kubelet up.
	consistently(t, "one CA to be not enough", 4*time.Second, func() error {
		err := k8sReader.Get(testCtx, types.NamespacedName{
			Namespace: "tenant-twk1", Name: "twk1-workers",
		}, template)
		if err == nil {
			return fmt.Errorf("written with only the Talos CA")
		}
		if !apierrors.IsNotFound(err) {
			return err
		}
		return nil
	})

	issueCA(t, "tenant-twk1", "twk1-ca", "ca.crt")
	eventually(t, "the worker config", func() error {
		return k8sReader.Get(testCtx, types.NamespacedName{
			Namespace: "tenant-twk1", Name: "twk1-workers",
		}, template)
	})

	config := workerConfig(t, "tenant-twk1", "twk1-workers")
	machine, _ := config["machine"].(map[string]any)
	cluster, _ := config["cluster"].(map[string]any)
	if machine == nil || cluster == nil {
		t.Fatalf("config = %v", config)
	}
	encoded := base64.StdEncoding.EncodeToString([]byte("a-certificate"))
	if fmt.Sprint(machine["ca"]) != fmt.Sprintf("map[crt:%s]", encoded) {
		t.Errorf("machine.ca = %v — Talos wants it base64, and the API client "+
			"has already decoded it", machine["ca"])
	}
	if fmt.Sprint(cluster["ca"]) != fmt.Sprintf("map[crt:%s]", encoded) {
		t.Errorf("cluster.ca = %v", cluster["ca"])
	}
}

// TestAVPCWorkerJoinsByAddressAndPinsTheNameToIt.
//
// A VPC worker can use nothing but the address: the name resolves through
// cluster DNS to a ClusterIP, and an isolated VPC has neither. The host entry
// pins the name inside the node anyway, because the node has no working DNS
// until it has joined.
func TestAVPCWorkerJoinsByAddressAndPinsTheNameToIt(t *testing.T) {
	mustKubeOVNResolver(t)
	mustTenant(t, vpcTalosTenant("twk2"))
	eventually(t, "the request for an address", func() error {
		_, err := cpService("twk2")
		return err
	})
	assignAddress(t, "twk2", "10.199.0.122")
	issueCA(t, "tenant-twk2", "twk2-talos-ca", "tls.crt")
	issueCA(t, "tenant-twk2", "twk2-ca", "ca.crt")

	eventually(t, "the worker config", func() error {
		template := &unstructured.Unstructured{}
		template.SetGroupVersionKind(talosConfigTemplateGVK)
		return k8sReader.Get(testCtx, types.NamespacedName{
			Namespace: "tenant-twk2", Name: "twk2-workers",
		}, template)
	})
	config := workerConfig(t, "tenant-twk2", "twk2-workers")
	cluster, _ := config["cluster"].(map[string]any)
	controlPlane, _ := cluster["controlPlane"].(map[string]any)
	if got := fmt.Sprint(controlPlane["endpoint"]); got != "https://10.199.0.122:6443" {
		t.Errorf("endpoint = %s", got)
	}

	machine, _ := config["machine"].(map[string]any)
	network, _ := machine["network"].(map[string]any)
	entries := fmt.Sprint(network["extraHostEntries"])
	if !strings.Contains(entries, "10.199.0.122") ||
		!strings.Contains(entries, "twk2-talos.tenant-twk2.svc") {
		t.Errorf("extraHostEntries = %s — the node has no DNS until it joins", entries)
	}

	// kubePrism proxies the apiserver via localhost, which would bypass the
	// name and take the SNI with it.
	features, _ := machine["features"].(map[string]any)
	prism, _ := features["kubePrism"].(map[string]any)
	if prism["enabled"] != false {
		t.Errorf("kubePrism = %v", prism)
	}

	// The clock comes from the tenant's own address first: it is the one that
	// works with no egress at all.
	timeConfig, _ := machine["time"].(map[string]any)
	servers := fmt.Sprint(timeConfig["servers"])
	if !strings.HasPrefix(servers, "[10.199.0.122") {
		t.Errorf("time.servers = %s", servers)
	}

	// The kubelet is pinned to the tenant's Kubernetes version, not the image's.
	// Talos ships whatever kubelet matches its own release, and against an
	// older control plane that is several minors of skew: the node boots,
	// reports healthy, and never registers.
	kubelet, _ := machine["kubelet"].(map[string]any)
	if got := fmt.Sprint(kubelet["image"]); !strings.HasSuffix(got, ":v1.33.1") {
		t.Errorf("kubelet image = %s", got)
	}
}

// TestEachWorkerClonesItsOwnRootFromTheSharedGolden.
//
// This used to point every worker at the golden PVC by name, and the
// consequences were measured: with one worker the golden stops being golden
// because the node writes into it; with two it is two writers on one block
// device; and deleting worker A takes the DataVolume with it, which silently
// wipes worker B's root during a rolling update.
func TestEachWorkerClonesItsOwnRootFromTheSharedGolden(t *testing.T) {
	mustKubeOVNResolver(t)
	mustTenant(t, vpcTalosTenant("twk3"))
	eventually(t, "the request for an address", func() error {
		_, err := cpService("twk3")
		return err
	})
	assignAddress(t, "twk3", "10.199.0.123")
	issueCA(t, "tenant-twk3", "twk3-talos-ca", "tls.crt")
	issueCA(t, "tenant-twk3", "twk3-ca", "ca.crt")

	template := theMachineTemplate(t, "tenant-twk3")

	disks, found, _ := unstructured.NestedSlice(template.Object, "spec", "template",
		"spec", "virtualMachineTemplate", "spec", "dataVolumeTemplates")
	if !found || len(disks) != 1 {
		t.Fatalf("dataVolumeTemplates = %v", disks)
	}
	disk, _ := disks[0].(map[string]any)
	spec, _ := disk["spec"].(map[string]any)
	source, _ := spec["source"].(map[string]any)
	pvc, _ := source["pvc"].(map[string]any)
	if pvc["name"] != "talos-golden-1-13-8" || pvc["namespace"] != "kubevirt-ui-system" {
		t.Errorf("source = %v — it clones the shared golden, and crossing that "+
			"namespace is the whole point of one import per release", pvc)
	}
	// `storage`, not `pvc`: CDI then takes the clone strategy from the target's
	// storage profile rather than being told one it may not support.
	if _, hasStorage := spec["storage"]; !hasStorage {
		t.Errorf("spec = %v", spec)
	}

	// A VPC worker's launcher pod is pinned into the tenant's subnet, and CAPK
	// cannot then SSH into it — so the bootstrap check has to be skipped, or the
	// deployment reads zero-ready with healthy nodes.
	if strategy, _, _ := unstructured.NestedString(template.Object, "spec", "template",
		"spec", "virtualMachineBootstrapCheck", "checkStrategy"); strategy != "none" {
		t.Errorf("checkStrategy = %q", strategy)
	}
	annotations, _, _ := unstructured.NestedStringMap(template.Object, "spec", "template",
		"spec", "virtualMachineTemplate", "spec", "template", "metadata", "annotations")
	if annotations["ovn.kubernetes.io/logical_switch"] != "net-twk3-default" {
		t.Errorf("annotations = %v", annotations)
	}
}

// TestTheHealthCheckWindowIsDerivedFromTheMeasurement.
//
// Three times the observed return, so the number moves when the measurement
// does instead of being re-guessed. The previous five minutes left ninety
// seconds of margin, and the check did fire during an ordinary reboot — it
// started its clock and only failed to remediate because the node beat it.
func TestTheHealthCheckWindowIsDerivedFromTheMeasurement(t *testing.T) {
	mustKubeOVNResolver(t)
	mustTenant(t, vpcTalosTenant("twk4"))
	eventually(t, "the request for an address", func() error {
		_, err := cpService("twk4")
		return err
	})
	assignAddress(t, "twk4", "10.199.0.124")
	issueCA(t, "tenant-twk4", "twk4-talos-ca", "tls.crt")
	issueCA(t, "tenant-twk4", "twk4-ca", "ca.crt")

	eventually(t, "the health check", func() error {
		check := &unstructured.Unstructured{}
		check.SetGroupVersionKind(machineHealthCheckGVK)
		return k8sReader.Get(testCtx, types.NamespacedName{
			Namespace: "tenant-twk4", Name: "twk4-workers",
		}, check)
	})
	check := &unstructured.Unstructured{}
	check.SetGroupVersionKind(machineHealthCheckGVK)
	if err := k8sClient.Get(testCtx, types.NamespacedName{
		Namespace: "tenant-twk4", Name: "twk4-workers",
	}, check); err != nil {
		t.Fatalf("reading the health check: %v", err)
	}

	conditions, _, _ := unstructured.NestedSlice(check.Object, "spec", "unhealthyConditions")
	if len(conditions) != 2 {
		t.Fatalf("unhealthyConditions = %v", conditions)
	}
	for _, raw := range conditions {
		condition, _ := raw.(map[string]any)
		if condition["timeout"] != "9m" {
			t.Errorf("timeout = %v, want three times the measured three-minute "+
				"return", condition["timeout"])
		}
	}
	// The window must stay inside the startup timeout, or a node that has never
	// joined is judged by a clock meant for one that has.
	startup, _, _ := unstructured.NestedString(check.Object, "spec", "nodeStartupTimeout")
	if startup != "20m" {
		t.Errorf("nodeStartupTimeout = %q", startup)
	}
	// A one-worker tenant is the common case, and the usual guard would refuse
	// to remediate the only worker — exactly when the tenant is fully down.
	if max, _, _ := unstructured.NestedString(check.Object, "spec", "maxUnhealthy"); max != "100%" {
		t.Errorf("maxUnhealthy = %q", max)
	}
}

// TestACloudInitPoolIsRefusedRatherThanHalfBuilt.
func TestACloudInitPoolIsRefusedRatherThanHalfBuilt(t *testing.T) {
	mustTenant(t, plainTenant("twk5"))

	eventually(t, "the tenant to say so", func() error {
		condition := tenantCondition(getTenant(t, "twk5"),
			platformv1alpha1.ConditionWorkersReady)
		if condition == nil {
			return fmt.Errorf("no condition")
		}
		if condition.Reason != "CloudInitNotMigrated" {
			return fmt.Errorf("reason = %q", condition.Reason)
		}
		return nil
	})
	deployment := &unstructured.Unstructured{}
	deployment.SetGroupVersionKind(machineDeploymentObjectGVK)
	err := k8sReader.Get(testCtx, types.NamespacedName{
		Namespace: "tenant-twk5", Name: "twk5-workers",
	}, deployment)
	if err == nil {
		t.Error("it built half a pool for an OS it cannot bootstrap")
	} else if !apierrors.IsNotFound(err) {
		t.Fatalf("reading the MachineDeployment: %v", err)
	}
}

// TestTheWorkerResolvesThroughItsOwnNetworksResolver.
//
// A worker in a VPC must use the VpcDns address first: the public resolvers are
// only reachable once the VPC has egress, and a tenant is normally created
// before any gateway is attached to it. Found by diffing against a live tenant
// — the operator's first worker config had the public list alone, which is a
// node that cannot resolve anything until something else is fixed.
//
// Read off the network's own status rather than an environment variable: the
// network already resolved it from kube-ovn's configuration and published it,
// and a second copy would agree until it did not.
func TestTheWorkerResolvesThroughItsOwnNetworksResolver(t *testing.T) {
	network := &platformv1alpha1.ManagedNetwork{
		ObjectMeta: metav1.ObjectMeta{Name: "net-twk6"},
		Spec:       platformv1alpha1.ManagedNetworkSpec{CIDR: "10.200.60.0/22"},
	}
	if err := k8sClient.Create(testCtx, network); err != nil && !apierrors.IsAlreadyExists(err) {
		t.Fatalf("creating the network: %v", err)
	}
	// The resolver the network controller would publish. Through eventually
	// because the client reads from a cache and the network controller is also
	// writing to this object.
	eventually(t, "the resolver to be published", func() error {
		live := &platformv1alpha1.ManagedNetwork{}
		if err := k8sReader.Get(testCtx, types.NamespacedName{Name: "net-twk6"}, live); err != nil {
			return err
		}
		if live.Status.DNSServer == "10.96.0.200" {
			return nil
		}
		live.Status.DNSServer = "10.96.0.200"
		return k8sClient.Status().Update(testCtx, live)
	})

	mustTenant(t, vpcTalosTenant("twk6"))
	eventually(t, "the request for an address", func() error {
		_, err := cpService("twk6")
		return err
	})
	assignAddress(t, "twk6", "10.199.0.126")
	issueCA(t, "tenant-twk6", "twk6-talos-ca", "tls.crt")
	issueCA(t, "tenant-twk6", "twk6-ca", "ca.crt")

	eventually(t, "the worker config", func() error {
		template := &unstructured.Unstructured{}
		template.SetGroupVersionKind(talosConfigTemplateGVK)
		return k8sReader.Get(testCtx, types.NamespacedName{
			Namespace: "tenant-twk6", Name: "twk6-workers",
		}, template)
	})
	config := workerConfig(t, "tenant-twk6", "twk6-workers")
	machine, _ := config["machine"].(map[string]any)
	network2, _ := machine["network"].(map[string]any)
	resolvers := fmt.Sprint(network2["nameservers"])
	if !strings.HasPrefix(resolvers, "[10.96.0.200") {
		t.Errorf("nameservers = %s — the VPC resolver has to come first, "+
			"because the public ones are unreachable until egress exists",
			resolvers)
	}
}

// TestTheRootClonesLandOnTheTenantsStorageClass.
//
// Not the golden's. That one is meant for an erasure-coded pool — read-only
// reference data cloned many times — while the clones want replica. The first
// version of this sent them wherever the golden lives, which the live diff
// showed as a missing `ceph-block`.
func TestTheRootClonesLandOnTheTenantsStorageClass(t *testing.T) {
	mustKubeOVNResolver(t)
	obj := vpcTalosTenant("twk7")
	obj.Spec.Storage.ClassName = "ceph-block"
	mustTenant(t, obj)
	eventually(t, "the request for an address", func() error {
		_, err := cpService("twk7")
		return err
	})
	assignAddress(t, "twk7", "10.199.0.127")
	issueCA(t, "tenant-twk7", "twk7-talos-ca", "tls.crt")
	issueCA(t, "tenant-twk7", "twk7-ca", "ca.crt")

	// By whatever name: the template's name carries a digest of the shape now,
	// so that a resize rotates it and CAPI rolls the pool. Looking it up by a
	// fixed name would tie this test to that naming rather than to the disk it
	// is about.
	var template *unstructured.Unstructured
	eventually(t, "the machine template", func() error {
		list := &unstructured.UnstructuredList{}
		list.SetGroupVersionKind(kubevirtMachineTemplateGVK.GroupVersion().
			WithKind(kubevirtMachineTemplateGVK.Kind + "List"))
		if err := k8sReader.List(testCtx, list,
			client.InNamespace("tenant-twk7")); err != nil {
			return err
		}
		if len(list.Items) != 1 {
			return fmt.Errorf("%d templates", len(list.Items))
		}
		template = &list.Items[0]
		return nil
	})
	disks, _, _ := unstructured.NestedSlice(template.Object, "spec", "template",
		"spec", "virtualMachineTemplate", "spec", "dataVolumeTemplates")
	if len(disks) != 1 {
		t.Fatalf("dataVolumeTemplates = %v", disks)
	}
	disk, _ := disks[0].(map[string]any)
	spec, _ := disk["spec"].(map[string]any)
	storage, _ := spec["storage"].(map[string]any)
	if storage["storageClassName"] != "ceph-block" {
		t.Errorf("storageClassName = %v", storage["storageClassName"])
	}
}

// theMachineTemplate is the one template in a tenant's namespace, by whatever
// name. The name carries a digest of the shape now — that is what makes a
// resize roll the pool — so looking it up by a fixed string would tie these
// tests to the naming rather than to what they are about.
func theMachineTemplate(t *testing.T, namespace string) *unstructured.Unstructured {
	t.Helper()
	var found *unstructured.Unstructured
	eventually(t, "the machine template", func() error {
		list := &unstructured.UnstructuredList{}
		list.SetGroupVersionKind(kubevirtMachineTemplateGVK.GroupVersion().
			WithKind(kubevirtMachineTemplateGVK.Kind + "List"))
		if err := k8sReader.List(testCtx, list,
			client.InNamespace(namespace)); err != nil {
			return err
		}
		if len(list.Items) != 1 {
			return fmt.Errorf("%d templates in %s", len(list.Items), namespace)
		}
		found = &list.Items[0]
		return nil
	})
	return found
}

// workerReconciler is the tenant reconciler with nothing site-specific set:
// these tests are about the shape of what it writes.
func workerReconciler() *ManagedTenantReconciler {
	return &ManagedTenantReconciler{
		Client: k8sClient, Scheme: k8sClient.Scheme(), APIReader: k8sReader,
	}
}

// TestResizingTheWorkersRollsThem.
//
// Scaling and resizing did not work, and the resize failed in the way that is
// hardest to see: the field accepted the patch and nothing happened. CAPI rolls
// a MachineDeployment when its template *reference* changes, so editing the
// template in place gives the new shape to the next worker created and none of
// the ones running.
func TestResizingTheWorkersRollsThem(t *testing.T) {
	mustNamespace(t, "tenant-trs1", "")
	obj := talosTenant("trs1")
	obj.Spec.Workers.VCPU = 2
	reconciler := workerReconciler()

	first, err := reconciler.ensureMachineTemplate(testCtx, obj, "tenant-trs1", "1.13.8")
	if err != nil {
		t.Fatalf("first pass: %v", err)
	}
	if err := reconciler.ensureMachineDeployment(
		testCtx, obj, "tenant-trs1", "trs1-workers", first); err != nil {
		t.Fatalf("pool: %v", err)
	}

	// Same shape, same name: adoption and every quiet pass depend on this.
	again, err := reconciler.ensureMachineTemplate(testCtx, obj, "tenant-trs1", "1.13.8")
	if err != nil || again != first {
		t.Fatalf("an unchanged shape rotated the template: %q → %q (%v)",
			first, again, err)
	}

	obj.Spec.Workers.VCPU = 4
	resized, err := reconciler.ensureMachineTemplate(testCtx, obj, "tenant-trs1", "1.13.8")
	if err != nil {
		t.Fatalf("resize: %v", err)
	}
	if resized == first {
		t.Fatal("the shape changed and the name did not, so nothing would roll")
	}
	if err := reconciler.ensureMachineDeployment(
		testCtx, obj, "tenant-trs1", "trs1-workers", resized); err != nil {
		t.Fatalf("pool after resize: %v", err)
	}

	pool := &unstructured.Unstructured{}
	pool.SetGroupVersionKind(machineDeploymentObjectGVK)
	if err := k8sReader.Get(testCtx, types.NamespacedName{
		Namespace: "tenant-trs1", Name: "trs1-workers"}, pool); err != nil {
		t.Fatalf("reading the pool: %v", err)
	}
	pointsAt, _, _ := unstructured.NestedString(pool.Object,
		"spec", "template", "spec", "infrastructureRef", "name")
	if pointsAt != resized {
		t.Errorf("the pool still stamps from %q", pointsAt)
	}

	// And back: the previous shape lands on the object it already had.
	obj.Spec.Workers.VCPU = 2
	back, err := reconciler.ensureMachineTemplate(testCtx, obj, "tenant-trs1", "1.13.8")
	if err != nil {
		t.Fatalf("resize back: %v", err)
	}
	if back != first {
		t.Errorf("going back minted a third template: %q, was %q", back, first)
	}
}

// TestAdoptingATenantDoesNotRollItsWorkers.
//
// The pool a product built points at `<tenant>-workers`, which is not the name
// this operator would choose. Renaming it on the first pass would roll every
// worker of a tenant that was only being described.
func TestAdoptingATenantDoesNotRollItsWorkers(t *testing.T) {
	mustNamespace(t, "tenant-trs2", "")
	obj := talosTenant("trs2")
	reconciler := workerReconciler()

	// What the operator would write, under the name the product uses.
	want, err := reconciler.machineTemplateSpec(obj, "1.13.8")
	if err != nil {
		t.Fatalf("building the shape: %v", err)
	}
	existing := &unstructured.Unstructured{}
	existing.SetGroupVersionKind(kubevirtMachineTemplateGVK)
	existing.SetName("trs2-workers")
	existing.SetNamespace("tenant-trs2")
	if err := unstructured.SetNestedMap(existing.Object, want, "spec"); err != nil {
		t.Fatalf("setting the spec: %v", err)
	}
	if err := k8sClient.Create(testCtx, existing); err != nil {
		t.Fatalf("creating the product's template: %v", err)
	}
	if err := reconciler.ensureMachineDeployment(
		testCtx, obj, "tenant-trs2", "trs2-workers", "trs2-workers"); err != nil {
		t.Fatalf("the product's pool: %v", err)
	}

	got, err := reconciler.ensureMachineTemplate(testCtx, obj, "tenant-trs2", "1.13.8")
	if err != nil {
		t.Fatalf("adopting: %v", err)
	}
	if got != "trs2-workers" {
		t.Errorf("adoption renamed the template to %q, which rolls every worker", got)
	}
}
