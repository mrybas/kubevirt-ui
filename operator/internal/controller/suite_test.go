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
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	utilruntime "k8s.io/apimachinery/pkg/util/runtime"
	clientgoscheme "k8s.io/client-go/kubernetes/scheme"
	"k8s.io/client-go/rest"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/envtest"
	logzap "sigs.k8s.io/controller-runtime/pkg/log/zap"
	metricsserver "sigs.k8s.io/controller-runtime/pkg/metrics/server"
	cdiv1 "kubevirt.io/containerized-data-importer-api/pkg/apis/core/v1beta1"

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
	testCtx   context.Context
	stopMgr   context.CancelFunc
)

func TestMain(m *testing.M) {
	code, err := runSuite(m)
	if err != nil {
		fmt.Fprintf(os.Stderr, "test suite setup failed: %v\n", err)
		os.Exit(1)
	}
	os.Exit(code)
}

func runSuite(m *testing.M) (int, error) {
	ctrl.SetLogger(logzap.New(logzap.UseDevMode(true), logzap.WriteTo(os.Stderr)))

	scheme := runtime.NewScheme()
	utilruntime.Must(clientgoscheme.AddToScheme(scheme))
	utilruntime.Must(platformv1alpha1.AddToScheme(scheme))
	utilruntime.Must(cdiv1.AddToScheme(scheme))

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

	mgr, err := ctrl.NewManager(cfg, ctrl.Options{
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
		return 0, fmt.Errorf("wiring controller: %w", err)
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
	k8sClient = mgr.GetClient()

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
