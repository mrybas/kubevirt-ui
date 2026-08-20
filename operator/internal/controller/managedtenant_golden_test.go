package controller

import (
	"fmt"
	"strings"
	"testing"
	"time"

	"github.com/prometheus/client_golang/prometheus/testutil"
	rbacv1 "k8s.io/api/rbac/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	cdiv1 "kubevirt.io/containerized-data-importer-api/pkg/apis/core/v1beta1"
	"sigs.k8s.io/controller-runtime/pkg/client"

	platformv1alpha1 "github.com/mrybas/kubevirt-ui/operator/api/v1alpha1"
	"github.com/mrybas/kubevirt-ui/operator/internal/metrics"
)

func talosTenant(name string) *platformv1alpha1.ManagedTenant {
	obj := plainTenant(name)
	obj.Spec.Workers.OS = "talos"
	return obj
}

// vpcTenant is a tenant in a network of its own, which is what makes it need an
// address of its own: on the default overlay the control plane is reached by
// the Kamaji Service's ClusterIP and no VIP is handed out at all.
func vpcTenant(name string) *platformv1alpha1.ManagedTenant {
	obj := plainTenant(name)
	obj.Spec.Network = "net-" + name
	return obj
}

func vpcTalosTenant(name string) *platformv1alpha1.ManagedTenant {
	obj := vpcTenant(name)
	obj.Spec.Workers.OS = "talos"
	return obj
}

func goldenImages(t *testing.T) []platformv1alpha1.ManagedImage {
	t.Helper()
	images := &platformv1alpha1.ManagedImageList{}
	if err := k8sClient.List(testCtx, images,
		client.InNamespace("kubevirt-ui-system"),
		client.MatchingLabels{"kubevirt-ui.io/talos-golden": "true"}); err != nil {
		t.Fatalf("listing golden images: %v", err)
	}
	return images.Items
}

// TestTwoTenantsOfOneVersionShareOneImport is H2, the reason the shared golden
// exists at all: the second tenant of a version imports nothing.
//
// The mechanism is the derived name — two tenants asking for one release ask
// for one object — so what the test has to prove is that neither tenant writes
// a second image, and that the disk behind it is not re-imported either.
func TestTwoTenantsOfOneVersionShareOneImport(t *testing.T) {
	mustTenant(t, talosTenant("tga"))

	eventually(t, "the first tenant's image", func() error {
		images := goldenImages(t)
		if len(images) != 1 {
			return fmt.Errorf("images = %d", len(images))
		}
		if images[0].Name != "talos-golden-1-13-8" {
			return fmt.Errorf("image named %q", images[0].Name)
		}
		return nil
	})

	// What the image controller made of it, so the second tenant is arriving at
	// something that already exists rather than racing its creation.
	eventually(t, "the disk behind it", func() error {
		dv := &cdiv1.DataVolume{}
		return k8sClient.Get(testCtx, types.NamespacedName{
			Namespace: "kubevirt-ui-system", Name: "talos-golden-1-13-8",
		}, dv)
	})
	first := &cdiv1.DataVolume{}
	if err := k8sClient.Get(testCtx, types.NamespacedName{
		Namespace: "kubevirt-ui-system", Name: "talos-golden-1-13-8",
	}, first); err != nil {
		t.Fatalf("reading the golden disk: %v", err)
	}

	writesBefore := goldenWrites()
	mustTenant(t, talosTenant("tgb"))

	eventually(t, "the second tenant to have its clone grant", func() error {
		binding := &rbacv1.RoleBinding{}
		return k8sClient.Get(testCtx, types.NamespacedName{
			Namespace: "kubevirt-ui-system",
			Name:      "talos-golden-cloner-tenant-tgb",
		}, binding)
	})

	consistently(t, "one image and one import", 5*time.Second, func() error {
		if images := goldenImages(t); len(images) != 1 {
			names := []string{}
			for _, image := range images {
				names = append(names, image.Name)
			}
			return fmt.Errorf("images = %v", names)
		}
		dv := &cdiv1.DataVolume{}
		if err := k8sClient.Get(testCtx, types.NamespacedName{
			Namespace: "kubevirt-ui-system", Name: "talos-golden-1-13-8",
		}, dv); err != nil {
			return err
		}
		// A re-import would be a new object, and a rewritten spec would be a
		// new resourceVersion on this one. Neither is allowed to happen.
		if dv.UID != first.UID {
			return fmt.Errorf("the golden disk was replaced: %s -> %s", first.UID, dv.UID)
		}
		if dv.ResourceVersion != first.ResourceVersion {
			return fmt.Errorf("the golden disk was rewritten: %s -> %s",
				first.ResourceVersion, dv.ResourceVersion)
		}
		return nil
	})

	// The second tenant declared the same image and the declaration was
	// identical, so the tenant controller wrote nothing to it. This is the part
	// resourceVersion alone cannot show: the API server normalises a no-op
	// update back to identical bytes, so only the counter knows.
	if after := goldenWrites(); after != writesBefore {
		t.Errorf("the second tenant wrote to the shared image %v times",
			after-writesBefore)
	}

	// And the grants are per tenant rather than one that keeps being rewritten:
	// two bindings on one role, each naming its own namespace.
	for _, namespace := range []string{"tenant-tga", "tenant-tgb"} {
		binding := &rbacv1.RoleBinding{}
		if err := k8sClient.Get(testCtx, types.NamespacedName{
			Namespace: "kubevirt-ui-system",
			Name:      "talos-golden-cloner-" + namespace,
		}, binding); err != nil {
			t.Fatalf("the clone grant for %s: %v", namespace, err)
		}
		if binding.RoleRef.Name != "talos-golden-cloner" {
			t.Errorf("%s bound to %q", namespace, binding.RoleRef.Name)
		}
		if len(binding.Subjects) != 1 ||
			binding.Subjects[0].Namespace != namespace ||
			binding.Subjects[0].Name != "default" {
			t.Errorf("%s granted to %+v — CDI evaluates the tenant namespace's "+
				"default ServiceAccount, not ours", namespace, binding.Subjects)
		}
	}
}

// goldenWrites is what the tenant controller has written to shared images.
// Only its own writes: the image controller writes to them too, and that is its
// job.
func goldenWrites() float64 {
	var sum float64
	for _, op := range []string{"created", "updated"} {
		sum += testutil.ToFloat64(metrics.PatchesTotal.WithLabelValues(
			"ManagedImage", tenantControllerName, op))
	}
	return sum
}

// TestACloudInitTenantDeclaresNoImage.
//
// It clones nothing, so a GoldenReady condition on it could only ever be a
// permanent false — a tenant that reads as half-broken for a disk it will never
// use.
func TestACloudInitTenantDeclaresNoImage(t *testing.T) {
	mustTenant(t, plainTenant("tgc"))

	eventually(t, "the tenant to settle", func() error {
		obj := getTenant(t, "tgc")
		if tenantCondition(obj, platformv1alpha1.ConditionNamespaceReady) == nil {
			return fmt.Errorf("not reconciled yet")
		}
		return nil
	})
	if got := tenantCondition(getTenant(t, "tgc"),
		platformv1alpha1.ConditionGoldenReady); got != nil {
		t.Errorf("a cloud-init tenant reports %+v about a Talos image", got)
	}

	binding := &rbacv1.RoleBinding{}
	err := k8sClient.Get(testCtx, types.NamespacedName{
		Namespace: "kubevirt-ui-system",
		Name:      "talos-golden-cloner-tenant-tgc",
	}, binding)
	if err == nil {
		t.Error("a cloud-init tenant was granted a clone it never makes")
	} else if !apierrors.IsNotFound(err) {
		t.Fatalf("reading the clone grant: %v", err)
	}
}

// TestTheGoldenConditionWaitsRatherThanClaimingReady.
//
// There is no CDI in envtest, so nothing ever finishes importing. A True here
// would mean the condition is not reading the image at all — which is the way
// this kind of status usually breaks: it reports the write it made rather than
// the thing it wrote about.
func TestTheGoldenConditionWaitsRatherThanClaimingReady(t *testing.T) {
	obj := talosTenant("tgd")
	obj.Spec.Workers.TalosVersion = "1.13.8"
	mustTenant(t, obj)

	eventually(t, "the golden condition", func() error {
		if tenantCondition(getTenant(t, "tgd"),
			platformv1alpha1.ConditionGoldenReady) == nil {
			return fmt.Errorf("no condition yet")
		}
		return nil
	})
	condition := tenantCondition(getTenant(t, "tgd"),
		platformv1alpha1.ConditionGoldenReady)
	if condition.Status == metav1.ConditionTrue {
		t.Errorf("the image reports Ready with nothing to import it: %+v", condition)
	}
	if condition.Message == "" {
		t.Error("the condition says nothing about what is being waited for")
	}
}

// TestAReleaseWithNoImageSaysSoInsteadOfWaitingForever.
//
// Called directly, because the catalogue this deployment runs on has an image
// for everything it offers and a tenant asking for a release the catalogue does
// not have is refused earlier, at Accepted. The branch still has to hold: a
// catalogue entry can be edited to a version with no URL, and the difference
// between "importing" and "there is nothing to import" is the difference
// between waiting and waiting forever.
func TestAReleaseWithNoImageSaysSoInsteadOfWaitingForever(t *testing.T) {
	reconciler := &ManagedTenantReconciler{
		Client: k8sClient,
		Scheme: k8sClient.Scheme(),
	}
	obj := talosTenant("tge")
	ready, message, err := reconciler.reconcileGolden(
		testCtx, obj, "tenant-tge", "9.9.9")
	if err != nil {
		t.Fatalf("reconcileGolden: %v", err)
	}
	if ready {
		t.Error("a release with no image reported ready")
	}
	if !strings.Contains(message, "9.9.9") {
		t.Errorf("the message does not name the release: %q", message)
	}

	// And nothing was written for it.
	image := &platformv1alpha1.ManagedImage{}
	err = k8sClient.Get(testCtx, types.NamespacedName{
		Namespace: "kubevirt-ui-system", Name: "talos-golden-9-9-9",
	}, image)
	if err == nil {
		t.Error("an image was declared for a release with nothing behind it")
	} else if !apierrors.IsNotFound(err) {
		t.Fatalf("reading the image: %v", err)
	}
}
