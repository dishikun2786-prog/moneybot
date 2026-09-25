//go:build linux

package pin

import "testing"

func TestPinToCPU(t *testing.T) {
	unlock, err := PinToCPU(0)
	if err != nil {
		t.Fatalf("PinToCPU: %v", err)
	}
	defer unlock()
}

func TestRunPinned(t *testing.T) {
	ran := false
	if err := RunPinned(0, func() { ran = true }); err != nil {
		t.Fatalf("RunPinned: %v", err)
	}
	if !ran {
		t.Fatal("fn 未执行")
	}
}