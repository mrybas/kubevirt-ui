/*
Package tenant, folder ceiling.

The backend refuses a tenant that does not fit its folder before writing
anything, and the operator did not: it writes the same quota from the same
description without ever asking, so `spec.workers.count` edited on the CR —
kubectl, GitOps, anything that is not the API — grew the charge against the
folder with nothing in the way. Measured on the stand: poc-transit, a root
folder with no ceiling at all, ended up holding 72 CPU of quota, and every
namespace in it had been counted the whole time by a check that was never
called.

The arithmetic here is deliberately the same rule as `assert_within_folder_quota`,
and `test/parity/folder-ceiling.json` is the one statement both assert against.
The interesting failure is not either side being wrong but the two disagreeing:
a tenant refused by the API and admitted by its reconciler is worse than either
answer on its own.

Pure on purpose — no client, no context. What it costs is that the caller has
to gather the tree and the namespaces first; what it buys is that the table can
drive it directly.
*/
package tenant

import (
	"fmt"

	"k8s.io/apimachinery/pkg/api/resource"
)

// FolderNode is one folder as the `kubevirt-ui-folders` ConfigMap holds it.
// An empty quantity in Ceiling means the folder does not cap that dimension,
// which is not the same as capping it at zero.
type FolderNode struct {
	Parent  string
	Ceiling Ceiling
}

// Ceiling is what a folder caps, as quantity strings; "" is uncapped.
type Ceiling struct {
	CPU     string
	Memory  string
	Storage string
}

// NamespaceQuota is one namespace's charge against its folder: the sum of the
// `hard` requests over every ResourceQuota it carries. Two objects in one
// namespace are summed, because that is what the ceiling charges for.
type NamespaceQuota struct {
	Namespace string
	Folder    string
	CPU       string
	Memory    string
	Storage   string
}

// CeilingRefusal says which dimension did not fit and by how much.
type CeilingRefusal struct {
	Folder    string
	Dimension string
	Limit     string
	Allocated string
	Free      string
	Asked     string
}

func (r *CeilingRefusal) Error() string {
	return fmt.Sprintf(
		"%s quota %s for folder %q is already %s allocated; %s is free and this tenant asks for %s",
		r.Dimension, r.Limit, r.Folder, r.Allocated, r.Free, r.Asked)
}

type dimension struct {
	name string
	of   func(Ceiling) string
	from func(NamespaceQuota) string
	want func(Quota) resource.Quantity
}

var dimensions = []dimension{
	{"CPU", func(c Ceiling) string { return c.CPU },
		func(n NamespaceQuota) string { return n.CPU },
		func(q Quota) resource.Quantity { return q.CPU }},
	{"memory", func(c Ceiling) string { return c.Memory },
		func(n NamespaceQuota) string { return n.Memory },
		func(q Quota) resource.Quantity { return q.Memory }},
	{"storage", func(c Ceiling) string { return c.Storage },
		func(n NamespaceQuota) string { return n.Storage },
		func(q Quota) resource.Quantity { return q.Storage }},
}

// milliOf reads a quantity string in milli-units; an unreadable or absent one
// is zero, which is what an absent quota charges.
func milliOf(s string) int64 {
	if s == "" {
		return 0
	}
	q, err := resource.ParseQuantity(s)
	if err != nil {
		return 0
	}
	return q.MilliValue()
}

// ceilingHolder is the nearest folder at or above this one that caps the
// dimension.
//
// A folder silent about a dimension does not permit it: its parent counts the
// subtree's allocation for that dimension, so the parent's ceiling is the one
// the request has to fit under. Climbing is what makes that ceiling reachable.
func ceilingHolder(tree map[string]FolderNode, folder string, d dimension) (string, int64, bool) {
	seen := map[string]bool{}
	for name := folder; name != "" && !seen[name]; {
		seen[name] = true
		node, ok := tree[name]
		if !ok {
			return "", 0, false
		}
		if raw := d.of(node.Ceiling); raw != "" {
			return name, milliOf(raw), true
		}
		name = node.Parent
	}
	return "", 0, false
}

// allocatedUnder is everything a folder's ceiling already covers, sub-folders
// included.
//
// A child that declares a quota reserves exactly that much — its own ceiling is
// the promise the parent has already made to it, whether or not its namespaces
// have claimed it. A child without one contributes whatever its subtree has
// actually allocated. Per dimension, not per child: treating "declares a quota"
// as one decision for the whole child lets a sub-folder capped in CPU alone
// spend unlimited memory.
func allocatedUnder(
	tree map[string]FolderNode, namespaces []NamespaceQuota, folder string, d dimension,
) int64 {
	total := int64(0)
	for _, ns := range namespaces {
		if ns.Folder == folder {
			total += milliOf(d.from(ns))
		}
	}
	for name, node := range tree {
		if node.Parent != folder {
			continue
		}
		if raw := d.of(node.Ceiling); raw != "" {
			total += milliOf(raw)
			continue
		}
		total += allocatedUnder(tree, namespaces, name, d)
	}
	return total
}

// CheckCeiling refuses a reservation that would overrun the folder's ceiling.
//
// `asking` is the namespace the reservation is for; whatever it holds today is
// not competing with itself, and — the rule that lets an over-committed folder
// be climbed out of — a request that does not grow what it already holds is
// never refused for lack of room. The ceiling still binds upwards, so the only
// direction out is down.
//
// A nil return means it fits, or that nothing caps the dimension: a folder
// with no ceiling and no ancestor that has one constrains nothing, which is
// the state most folders are created in.
func CheckCeiling(
	tree map[string]FolderNode, namespaces []NamespaceQuota,
	folder, asking string, want Quota,
) error {
	if folder == "" {
		return nil
	}
	for _, d := range dimensions {
		holder, limit, ok := ceilingHolder(tree, folder, d)
		if !ok {
			continue
		}
		held := int64(0)
		for _, ns := range namespaces {
			if ns.Namespace == asking {
				held += milliOf(d.from(ns))
			}
		}
		asked := d.want(want)
		askedMilli := asked.MilliValue()
		if askedMilli <= held {
			continue
		}
		free := limit - (allocatedUnder(tree, namespaces, holder, d) - held)
		if free < 0 {
			free = 0
		}
		if askedMilli > free {
			return &CeilingRefusal{
				Folder:    holder,
				Dimension: d.name,
				Limit:     format(d, limit),
				Allocated: format(d, allocatedUnder(tree, namespaces, holder, d)-held),
				Free:      format(d, free),
				Asked:     format(d, askedMilli),
			}
		}
	}
	return nil
}

// format prints milli-units back in the shape the dimension is read in: cores
// for CPU, bytes for the rest.
func format(d dimension, milli int64) string {
	if d.name == "CPU" {
		return resource.NewMilliQuantity(milli, resource.DecimalSI).String()
	}
	return resource.NewQuantity(milli/1000, resource.BinarySI).String()
}
