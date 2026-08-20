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
	"sort"
	"strings"
	"time"

	corev1 "k8s.io/api/core/v1"
	apimeta "k8s.io/apimachinery/pkg/api/meta"
	"k8s.io/apimachinery/pkg/api/resource"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/client-go/tools/record"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/handler"
	"sigs.k8s.io/controller-runtime/pkg/reconcile"

	platformv1alpha1 "github.com/mrybas/kubevirt-ui/operator/api/v1alpha1"
	"github.com/mrybas/kubevirt-ui/operator/internal/kube"
	"github.com/mrybas/kubevirt-ui/operator/internal/talos"
	"github.com/mrybas/kubevirt-ui/operator/internal/tenant"
)

const (
	tenantControllerName = "managedtenant"

	// pendingRequeue is how often a tenant comes back to look at the two things
	// nobody wakes it for: the signer's secret and the shared image's import.
	pendingRequeue = 10 * time.Second

	// tenantCatalogEnv is where a deployment states its Talos releases — the
	// same variable the endpoint and the webhook read, so all three answer with
	// one list.
	tenantCatalogEnv = "TENANTS_TALOS_CATALOG"
)

// ManagedTenantReconciler builds the namespace a tenant lives in.
//
// This is phases 7 and 8 of the create it replaces: the namespace with its
// labels, one quota, and the LimitRange without which that quota stops the
// control plane from starting at all.
type ManagedTenantReconciler struct {
	client.Client
	Scheme   *runtime.Scheme
	Recorder record.EventRecorder

	// APIReader reads straight from the API server, for the two occasional
	// lookups — the MetalLB pool and the transit subnet — that are not worth a
	// cluster-wide watch apiece.
	APIReader client.Reader

	// Where tenant addresses come from, and the subnet that must exclude them.
	// Fields rather than environment reads at the point of use: the value
	// belongs to the deployment, and a test that has to set a process variable
	// to exercise one controller sets it for every other one running beside it.
	// Empty falls back to the environment, so the deployment can keep
	// configuring it the way the product always has.
	MetalLBPool      string
	MetalLBNamespace string
	TransitSubnet    string
}

// +kubebuilder:rbac:groups=platform.kubevirt-ui.io,resources=managedtenants,verbs=get;list;watch;create;update;patch;delete
// +kubebuilder:rbac:groups=platform.kubevirt-ui.io,resources=managedtenants/status,verbs=get;update;patch
// +kubebuilder:rbac:groups="",resources=namespaces;resourcequotas;limitranges,verbs=get;list;watch;create;update;patch
// +kubebuilder:rbac:groups=platform.kubevirt-ui.io,resources=managedimages,verbs=get;list;watch;create;update;patch
// +kubebuilder:rbac:groups=rbac.authorization.k8s.io,resources=roles;rolebindings,verbs=get;list;watch;create;update;patch
// Held so it can be granted: Kubernetes refuses to create a Role conferring
// permissions the writer does not have itself, and this controller's whole job
// here is handing `datavolumes/source` to each tenant's ServiceAccount. Nothing
// in the lab could show this — envtest and the dev backend both run as admin,
// where an escalation check never fires.
// +kubebuilder:rbac:groups=cdi.kubevirt.io,resources=datavolumes/source,verbs=create
// +kubebuilder:rbac:groups="",resources=services,verbs=get;list;watch;create;update;patch
// +kubebuilder:rbac:groups=metallb.io,resources=ipaddresspools,verbs=get;list;watch
// +kubebuilder:rbac:groups=cert-manager.io,resources=issuers;certificates,verbs=get;list;watch;create;update;patch
// +kubebuilder:rbac:groups="",resources=secrets;configmaps,verbs=get;list;watch
// +kubebuilder:rbac:groups="",resources=configmaps,verbs=create;update;patch
// +kubebuilder:rbac:groups=apps,resources=deployments,verbs=get;list;watch;create;update;patch
// +kubebuilder:rbac:groups=discovery.k8s.io,resources=endpointslices,verbs=get;list;watch
// +kubebuilder:rbac:groups=cluster.x-k8s.io,resources=clusters;machinehealthchecks,verbs=get;list;watch;create;update;patch
// +kubebuilder:rbac:groups=controlplane.cluster.x-k8s.io,resources=kamajicontrolplanes,verbs=get;list;watch;create;update;patch
// +kubebuilder:rbac:groups=infrastructure.cluster.x-k8s.io,resources=kubevirtclusters;kubevirtmachinetemplates,verbs=get;list;watch;create;update;patch
// +kubebuilder:rbac:groups=kubeovn.io,resources=subnets,verbs=get;list;watch

// Reconcile brings the tenant's namespace into line with the declaration.
func (r *ManagedTenantReconciler) Reconcile(
	ctx context.Context, req ctrl.Request,
) (ctrl.Result, error) {
	obj := &platformv1alpha1.ManagedTenant{}
	if err := r.Get(ctx, req.NamespacedName, obj); err != nil {
		return ctrl.Result{}, client.IgnoreNotFound(err)
	}
	if obj.Annotations[pausedAnnotation] == "true" || !obj.DeletionTimestamp.IsZero() {
		return ctrl.Result{}, nil
	}

	before := obj.DeepCopy()
	namespace := tenant.NamespaceOf(obj.Name)
	obj.Status.Namespace = namespace

	release, refusal := r.resolveRelease(obj)
	if refusal != "" {
		// Admission refuses this too, but an object can predate the webhook or
		// arrive while it is unreachable, and a tenant nobody can build should
		// say so rather than half-exist.
		r.setTenantCondition(obj, platformv1alpha1.ConditionTenantAccepted,
			false, "IncompatibleVersions", refusal)
		obj.Status.ObservedGeneration = obj.Generation
		return ctrl.Result{}, kube.UpdateStatus(ctx, r.Client, tenantControllerName, obj, before)
	}
	obj.Status.TalosRelease = release
	r.setTenantCondition(obj, platformv1alpha1.ConditionTenantAccepted, true, "Accepted",
		"the request can be built as written")

	reservation, err := tenant.Reserve(tenant.SizingOf(obj))
	if err != nil {
		r.setTenantCondition(obj, platformv1alpha1.ConditionQuotaReserved,
			false, "Unsizeable", err.Error())
		obj.Status.ObservedGeneration = obj.Generation
		return ctrl.Result{}, kube.UpdateStatus(ctx, r.Client, tenantControllerName, obj, before)
	}
	if err := r.ensureNamespace(ctx, obj, namespace); err != nil {
		r.setTenantCondition(obj, platformv1alpha1.ConditionNamespaceReady,
			false, "WriteFailed", err.Error())
		obj.Status.ObservedGeneration = obj.Generation
		_ = kube.UpdateStatus(ctx, r.Client, tenantControllerName, obj, before)
		return ctrl.Result{}, err
	}

	// The LimitRange goes in before the quota, and the order is the whole
	// point. A quota on requests makes requests mandatory, and Kamaji's
	// control-plane containers declare none — with the quota in place first,
	// every pod in the namespace is refused until the defaults arrive.
	if err := r.ensureLimitRange(ctx, obj, namespace); err != nil {
		r.setTenantCondition(obj, platformv1alpha1.ConditionNamespaceReady,
			false, "WriteFailed", err.Error())
		obj.Status.ObservedGeneration = obj.Generation
		_ = kube.UpdateStatus(ctx, r.Client, tenantControllerName, obj, before)
		return ctrl.Result{}, err
	}
	// What else caps storage here decides what this quota may say, so it is
	// read before the quota is written and not after.
	redundant, err := r.redundantStorageQuotas(ctx, namespace)
	if err != nil {
		return ctrl.Result{}, err
	}

	// One counter, two intents. The workers' disks and the allowance for the
	// tenant's own workloads both spend `requests.storage` in this namespace,
	// and Kubernetes requires every quota to be satisfied — so two objects mean
	// the effective cap is the smaller of the two while the folder ceiling,
	// which sums every quota it finds, charges for both.
	//
	// Measured on this stand: a tenant with `tenant-storage` at 100Gi and its
	// own quota at 120Gi was charged 220Gi against its folder and could use
	// 100. A 110Gi claim in that shape is refused by the smaller object; under
	// a single 220Gi quota the same claim is admitted. So one object, summed:
	// enforcement loosens from 100 to 220 deliberately, because 220 is what was
	// already being charged, and charging for capacity you forbid is the worse
	// of the two.
	//
	// Unless somebody else's quota is still there. Then adding the allowance
	// here would take the folder charge to 320 and change nothing about what is
	// enforced, since the other object still binds at its own number. In that
	// case this writes what the product writes today — the machines only — and
	// says what is wrong rather than making it worse.
	total := reservation
	if len(redundant) == 0 {
		total = tenant.WithStorageAllowance(reservation, storageAllowanceOf(obj))
	}
	obj.Status.Reservation = &platformv1alpha1.TenantReservation{
		CPU:     total.CPU.String(),
		Memory:  total.Memory.String(),
		Storage: total.Storage.String(),
	}

	if err := r.ensureQuota(ctx, obj, namespace, total, len(redundant) == 0); err != nil {
		r.setTenantCondition(obj, platformv1alpha1.ConditionQuotaReserved,
			false, "WriteFailed", err.Error())
		obj.Status.ObservedGeneration = obj.Generation
		_ = kube.UpdateStatus(ctx, r.Client, tenantControllerName, obj, before)
		return ctrl.Result{}, err
	}

	obj.Status.RedundantQuotas = redundant

	// After the namespace, because the Service lives in it.
	vip, addressNeeded, addressReady, addressMessage, err := r.reconcileAddress(
		ctx, obj, namespace)
	if err != nil {
		apimeta.SetStatusCondition(&obj.Status.Conditions,
			addressCondition(false, err.Error()))
		obj.Status.ObservedGeneration = obj.Generation
		_ = kube.UpdateStatus(ctx, r.Client, tenantControllerName, obj, before)
		return ctrl.Result{}, err
	}
	// Kept even while pending: an address that has been handed out is not
	// withdrawn because one read came back empty.
	if vip != "" {
		obj.Status.ControlPlaneVIP = vip
	}
	if addressNeeded {
		apimeta.SetStatusCondition(&obj.Status.Conditions,
			addressCondition(addressReady, addressMessage))
	}

	// Two of the things below are waited for rather than watched: cert-manager
	// writes the signer's secret, and the image controller finishes an import.
	// Watching either would mean caching every Secret and every ManagedImage in
	// the cluster to notice one object apiece, so the tenant comes back and
	// looks instead. It stops as soon as they are ready.
	pending := false

	// After the address, which the signer's certificate carries as an IP SAN.
	pkiReady, pkiReason, pkiMessage, err := r.reconcilePKI(
		ctx, obj, namespace, obj.Status.ControlPlaneVIP, addressNeeded)
	if err != nil {
		apimeta.SetStatusCondition(&obj.Status.Conditions,
			pkiCondition(false, "WriteFailed", err.Error()))
		obj.Status.ObservedGeneration = obj.Generation
		_ = kube.UpdateStatus(ctx, r.Client, tenantControllerName, obj, before)
		return ctrl.Result{}, err
	}
	if pkiMessage != "" || obj.Spec.Workers.OS == "talos" {
		apimeta.SetStatusCondition(&obj.Status.Conditions,
			pkiCondition(pkiReady, pkiReason, pkiMessage))
		pending = pending || !pkiReady
	}

	// On the same address as the API server, which is what makes it reachable
	// from a VPC with no egress at all.
	timeReady, timeReason, timeMessage := false, "", ""
	if addressNeeded {
		timeReady, timeReason, timeMessage, err = r.reconcileTime(
			ctx, obj, obj.Status.ControlPlaneVIP)
	}
	if err != nil {
		apimeta.SetStatusCondition(&obj.Status.Conditions,
			timeCondition(false, "WriteFailed", err.Error()))
		obj.Status.ObservedGeneration = obj.Generation
		_ = kube.UpdateStatus(ctx, r.Client, tenantControllerName, obj, before)
		return ctrl.Result{}, err
	}
	if addressNeeded {
		// Only a tenant with an address of its own has anywhere to serve the
		// time. On the default overlay a worker reaches the public servers the
		// same way it reaches everything else.
		apimeta.SetStatusCondition(&obj.Status.Conditions,
			timeCondition(timeReady, timeReason, timeMessage))
		pending = pending || !timeReady
	}

	// After the PKI, because the control plane mounts the signer's secrets, and
	// after the address, because that is what the workers will be told to join.
	cpReady, cpReason, cpMessage, err := r.reconcileControlPlane(
		ctx, obj, namespace, obj.Status.ControlPlaneVIP, addressNeeded)
	if err != nil {
		apimeta.SetStatusCondition(&obj.Status.Conditions,
			controlPlaneCondition(false, "WriteFailed", err.Error()))
		obj.Status.ObservedGeneration = obj.Generation
		_ = kube.UpdateStatus(ctx, r.Client, tenantControllerName, obj, before)
		return ctrl.Result{}, err
	}
	apimeta.SetStatusCondition(&obj.Status.Conditions,
		controlPlaneCondition(cpReady, cpReason, cpMessage))
	pending = pending || !cpReady

	// After the namespace, because the clone grant names it as its subject.
	goldenReady, goldenMessage, err := r.reconcileGolden(ctx, obj, namespace, release)
	if err != nil {
		apimeta.SetStatusCondition(&obj.Status.Conditions,
			goldenCondition(false, err.Error()))
		obj.Status.ObservedGeneration = obj.Generation
		_ = kube.UpdateStatus(ctx, r.Client, tenantControllerName, obj, before)
		return ctrl.Result{}, err
	}
	if goldenMessage != "" || obj.Spec.Workers.OS == "talos" {
		apimeta.SetStatusCondition(&obj.Status.Conditions,
			goldenCondition(goldenReady, goldenMessage))
		pending = pending || !goldenReady
	}

	r.setTenantCondition(obj, platformv1alpha1.ConditionNamespaceReady, true, "Ready",
		fmt.Sprintf("%s has its quota and its LimitRange", namespace))
	if len(redundant) > 0 {
		r.setTenantCondition(obj, platformv1alpha1.ConditionQuotaReserved,
			false, "CountedTwice",
			fmt.Sprintf("%s also caps storage in this namespace. Kubernetes "+
				"requires every quota to be satisfied, so what is actually "+
				"enforced is the smaller of them, while the folder ceiling sums "+
				"them and charges for both. This quota therefore carries the "+
				"machines only, without the workload allowance, which would "+
				"raise the charge and change nothing about the limit. Left in "+
				"place: something else wrote it",
				strings.Join(redundant, ", ")))
	} else {
		r.setTenantCondition(obj, platformv1alpha1.ConditionQuotaReserved, true, "Reserved",
			fmt.Sprintf("cpu %s, memory %s, storage %s",
				total.CPU.String(), total.Memory.String(), total.Storage.String()))
	}

	obj.Status.ObservedGeneration = obj.Generation
	result := ctrl.Result{}
	if pending {
		result.RequeueAfter = pendingRequeue
	}
	return result, kube.UpdateStatus(ctx, r.Client, tenantControllerName, obj, before)
}

// resolveRelease answers which Talos release this tenant builds from, or why
// it cannot.
func (r *ManagedTenantReconciler) resolveRelease(
	obj *platformv1alpha1.ManagedTenant,
) (release, refusal string) {
	if obj.Spec.Workers.OS != "talos" {
		return "", ""
	}
	entries, _ := talos.Catalog(os.Getenv(tenantCatalogEnv))
	version := obj.Spec.Workers.TalosVersion
	if version == "" {
		chosen, ok := talos.DefaultRelease(entries)
		if !ok {
			return "", "this deployment offers no Talos release at all"
		}
		version = chosen.Talos
	}
	if refusal := talos.Refusal(entries, version, obj.Spec.KubernetesVersion); refusal != "" {
		return "", refusal
	}
	return version, ""
}

// ensureNamespace writes the namespace a tenant's objects live in.
func (r *ManagedTenantReconciler) ensureNamespace(
	ctx context.Context, obj *platformv1alpha1.ManagedTenant, name string,
) error {
	live := &corev1.Namespace{}
	live.Name = name

	_, err := kube.Ensure(ctx, r.Client, tenantControllerName, live, func() error {
		if live.Labels == nil {
			live.Labels = map[string]string{}
		}
		for key, value := range tenant.NamespaceLabels(obj) {
			live.Labels[key] = value
		}
		// The logical switch is stamped before the namespace exists, and that
		// is deliberate: kube-ovn-controller default-claims a new namespace to
		// the cluster overlay the moment it sees one, so a pod born here would
		// land on 10.16/16 even though the tenant is attached to a VPC.
		if switchName := tenant.LogicalSwitchOf(obj); switchName != "" {
			if live.Annotations == nil {
				live.Annotations = map[string]string{}
			}
			live.Annotations["ovn.kubernetes.io/logical_switch"] = switchName
		}
		return nil
	})
	if err != nil {
		return fmt.Errorf("Namespace/%s: %w", name, err)
	}
	return nil
}

// ensureLimitRange supplies the requests Kamaji's containers do not declare.
//
// Only `defaultRequest`. A defaulted *limit* would throttle the apiserver at
// whatever number was picked here.
func (r *ManagedTenantReconciler) ensureLimitRange(
	ctx context.Context, obj *platformv1alpha1.ManagedTenant, namespace string,
) error {
	live := &corev1.LimitRange{}
	live.Name = namespace + "-limits"
	live.Namespace = namespace

	_, err := kube.Ensure(ctx, r.Client, tenantControllerName, live, func() error {
		if live.Labels == nil {
			live.Labels = map[string]string{}
		}
		live.Labels["kubevirt-ui.io/managed"] = "true"
		live.Labels["kubevirt-ui.io/tenant"] = obj.Name
		live.Spec.Limits = []corev1.LimitRangeItem{{
			Type: corev1.LimitTypeContainer,
			DefaultRequest: corev1.ResourceList{
				corev1.ResourceCPU:    resource.MustParse("50m"),
				corev1.ResourceMemory: resource.MustParse("128Mi"),
			},
		}}
		return nil
	})
	if err != nil {
		return fmt.Errorf("LimitRange/%s: %w", live.Name, err)
	}
	return nil
}

// ensureQuota writes the one quota.
//
// Requests only, never limits. A ResourceQuota that caps a limit makes the API
// server *require* that limit on every pod in the namespace, and the Kamaji
// control plane declares none — every tenant created in a folder simply had no
// control plane, the TenantControlPlane sat NotReady with zero pods, and the
// page reported Provisioning forever.
func (r *ManagedTenantReconciler) ensureQuota(
	ctx context.Context, obj *platformv1alpha1.ManagedTenant,
	namespace string, total tenant.Quota, ownsPVCCount bool,
) error {
	live := &corev1.ResourceQuota{}
	live.Name = namespace + "-quota"
	live.Namespace = namespace

	_, err := kube.Ensure(ctx, r.Client, tenantControllerName, live, func() error {
		if live.Labels == nil {
			live.Labels = map[string]string{}
		}
		live.Labels["kubevirt-ui.io/managed"] = "true"
		live.Labels["kubevirt-ui.io/tenant"] = obj.Name
		live.Spec.Hard = corev1.ResourceList{
			corev1.ResourceRequestsCPU:     total.CPU,
			corev1.ResourceRequestsMemory:  total.Memory,
			corev1.ResourceRequestsStorage: total.Storage,
		}
		// The PVC count lives here too, rather than in a second object — but
		// only when there is no second object. Two caps on the same counter is
		// the thing being undone, not something to add another instance of.
		if ownsPVCCount {
			live.Spec.Hard[corev1.ResourcePersistentVolumeClaims] =
				*resource.NewQuantity(int64(pvcCountOf(obj)), resource.DecimalSI)
		}
		return nil
	})
	if err != nil {
		return fmt.Errorf("ResourceQuota/%s: %w", live.Name, err)
	}
	return nil
}

// redundantStorageQuotas is every other quota in the namespace that also caps
// storage.
//
// Kubernetes applies quotas independently, so two storage caps mean the
// effective one is the smaller — but the folder ceiling sums every quota it
// finds, so the tenant is charged for its storage once per object. Measured on
// this stand: one tenant carrying both was counted 220Gi against its folder
// while reserving 120Gi, and the tenant beside it had only one, so the
// double-count was not even consistent.
func (r *ManagedTenantReconciler) redundantStorageQuotas(
	ctx context.Context, namespace string,
) ([]string, error) {
	list := &corev1.ResourceQuotaList{}
	if err := r.List(ctx, list, client.InNamespace(namespace)); err != nil {
		return nil, fmt.Errorf("listing quotas in %s: %w", namespace, err)
	}
	ours := namespace + "-quota"
	var out []string
	for i := range list.Items {
		if list.Items[i].Name == ours {
			continue
		}
		if _, caps := list.Items[i].Spec.Hard[corev1.ResourceRequestsStorage]; caps {
			out = append(out, list.Items[i].Name)
		}
	}
	sort.Strings(out)
	return out, nil
}

func storageAllowanceOf(obj *platformv1alpha1.ManagedTenant) int64 {
	allowance := obj.Spec.Storage.AllowanceGi
	if allowance == 0 {
		allowance = 100
	}
	return int64(allowance) << 30
}

func pvcCountOf(obj *platformv1alpha1.ManagedTenant) int32 {
	if obj.Spec.Storage.PVCCount == 0 {
		return 20
	}
	return obj.Spec.Storage.PVCCount
}

func (r *ManagedTenantReconciler) setTenantCondition(
	obj *platformv1alpha1.ManagedTenant, kind string, ok bool, reason, message string,
) {
	status := metav1.ConditionTrue
	if !ok {
		status = metav1.ConditionFalse
	}
	apimeta.SetStatusCondition(&obj.Status.Conditions, metav1.Condition{
		Type:               kind,
		Status:             status,
		Reason:             reason,
		Message:            message,
		ObservedGeneration: obj.Generation,
	})
}

// SetupWithManager wires the controller to what it writes, and to what other
// writers put in the same namespace.
func (r *ManagedTenantReconciler) SetupWithManager(mgr ctrl.Manager) error {
	// Ownership is not enough here. A quota this controller does not own is
	// exactly the one worth hearing about — it is what makes the folder charge
	// the tenant twice — and `Owns` would never deliver it, so the redundancy
	// would sit unnoticed until somebody read the namespace by hand.
	toTenant := handler.EnqueueRequestsFromMapFunc(
		func(ctx context.Context, obj client.Object) []reconcile.Request {
			name := strings.TrimPrefix(obj.GetNamespace(), "tenant-")
			if name == obj.GetNamespace() || name == "" {
				return nil
			}
			return []reconcile.Request{{
				NamespacedName: types.NamespacedName{Name: name},
			}}
		})

	return ctrl.NewControllerManagedBy(mgr).
		For(&platformv1alpha1.ManagedTenant{}).
		Watches(&corev1.ResourceQuota{}, toTenant).
		Watches(&corev1.LimitRange{}, toTenant).
		// The address arrives on the Service's status, written by MetalLB long
		// after this controller asked for it. Without this watch the tenant
		// would carry "no address yet" until something unrelated woke it —
		// which, with a ten-hour resync, is indistinguishable from never.
		Watches(&corev1.Service{}, toTenant).
		// The control plane's readiness is CAPI's answer and arrives on the
		// Cluster's status, long after this controller declared it. The
		// requeue while pending would catch the rising edge within ten
		// seconds, but nothing at all would catch the falling one — a control
		// plane that stops answering would leave the tenant reading Ready
		// until something unrelated woke it.
		Watches(clusterObject(), toTenant).
		Named(tenantControllerName).
		Complete(r)
}
