package controller

import (
	"context"
	"encoding/json"
	"fmt"

	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/api/resource"
	"k8s.io/apimachinery/pkg/types"
	"sigs.k8s.io/controller-runtime/pkg/client"

	platformv1alpha1 "github.com/mrybas/kubevirt-ui/operator/api/v1alpha1"
	"github.com/mrybas/kubevirt-ui/operator/internal/network"
	"github.com/mrybas/kubevirt-ui/operator/internal/tenant"
)

// foldersConfigMap is where the folder tree lives — one key per folder, each a
// JSON object with `parent_id` and an optional `quota`.
const foldersConfigMap = "kubevirt-ui-folders"

// ceilingRefusal is the folder's answer to what this tenant is about to
// reserve, or nil if it fits.
//
// The API refuses a tenant that does not fit before writing anything, and this
// reconciler did not: it writes the same quota from the same description, so
// `spec.workers.count` edited on the CR grew the charge against the folder
// with nothing in the way. Measured on the stand — poc-transit held 72 CPU of
// quota, every namespace of it counted the whole time by a check that was
// never called from here.
//
// Two reads for the whole cluster rather than one per namespace: the tree from
// the ConfigMap, then every namespace carrying a folder label and every
// ResourceQuota in one list apiece. A namespace with two quotas is summed,
// because summing is what the ceiling charges for.
//
// The second return is a real failure to *ask* — an unreadable tree or an API
// error. It is not a refusal, and it must not be treated as one: a ceiling
// that cannot be read has not said no.
func (r *ManagedTenantReconciler) ceilingRefusal(
	ctx context.Context, obj *platformv1alpha1.ManagedTenant,
	namespace string, total tenant.Quota,
) (error, error) {
	if obj.Spec.Folder == "" {
		return nil, nil
	}

	cm := &corev1.ConfigMap{}
	err := r.Get(ctx, types.NamespacedName{
		Namespace: systemNamespace, Name: foldersConfigMap,
	}, cm)
	if apierrors.IsNotFound(err) {
		// No folders exist at all, so nothing caps anything. This is the state
		// of an install that has never made one, not an error.
		return nil, nil
	}
	if err != nil {
		return nil, fmt.Errorf("reading %s/%s: %w", systemNamespace, foldersConfigMap, err)
	}

	tree := map[string]tenant.FolderNode{}
	for name, raw := range cm.Data {
		var folder struct {
			Parent string `json:"parent_id"`
			Quota  struct {
				CPU     string `json:"cpu"`
				Memory  string `json:"memory"`
				Storage string `json:"storage"`
			} `json:"quota"`
		}
		if err := json.Unmarshal([]byte(raw), &folder); err != nil {
			// One unreadable folder is not licence to ignore the rest of the
			// tree, but it is also not something to guess at: if it is the one
			// holding the ceiling, `CheckCeiling` finds no holder and lets the
			// reservation through, which is the behaviour of a folder without
			// a quota and the safer of the two mistakes.
			continue
		}
		tree[name] = tenant.FolderNode{
			Parent: folder.Parent,
			Ceiling: tenant.Ceiling{
				CPU:     folder.Quota.CPU,
				Memory:  folder.Quota.Memory,
				Storage: folder.Quota.Storage,
			},
		}
	}

	namespaces := &corev1.NamespaceList{}
	if err := r.List(ctx, namespaces, client.HasLabels{network.FolderLabel}); err != nil {
		return nil, fmt.Errorf("listing the folder's namespaces: %w", err)
	}
	folderOf := make(map[string]string, len(namespaces.Items))
	for _, ns := range namespaces.Items {
		folderOf[ns.Name] = ns.Labels[network.FolderLabel]
	}

	quotas := &corev1.ResourceQuotaList{}
	if err := r.List(ctx, quotas); err != nil {
		return nil, fmt.Errorf("listing resource quotas: %w", err)
	}
	sums := map[string]*tenant.Quota{}
	for i := range quotas.Items {
		q := &quotas.Items[i]
		if _, ours := folderOf[q.Namespace]; !ours {
			continue
		}
		sum, seen := sums[q.Namespace]
		if !seen {
			sum = &tenant.Quota{}
			sums[q.Namespace] = sum
		}
		add(&sum.CPU, q.Spec.Hard[corev1.ResourceRequestsCPU])
		add(&sum.Memory, q.Spec.Hard[corev1.ResourceRequestsMemory])
		add(&sum.Storage, q.Spec.Hard[corev1.ResourceRequestsStorage])
	}

	held := make([]tenant.NamespaceQuota, 0, len(sums))
	for name, sum := range sums {
		held = append(held, tenant.NamespaceQuota{
			Namespace: name,
			Folder:    folderOf[name],
			CPU:       sum.CPU.String(),
			Memory:    sum.Memory.String(),
			Storage:   sum.Storage.String(),
		})
	}

	return tenant.CheckCeiling(tree, held, obj.Spec.Folder, namespace, total), nil
}

func add(into *resource.Quantity, value resource.Quantity) {
	into.Add(value)
}
