//go:build !linux

package pin

import "runtime"

// PinToCPU 非 Linux 平台为 no-op（sched_setaffinity 仅 Linux 支持）。
func PinToCPU(cpu int) (func(), error) {
	return func() {}, nil
}

// RunPinned 非 Linux 平台直接在普通 goroutine 里运行 fn。
func RunPinned(cpu int, fn func()) error {
	fn()
	return nil
}

// CPUCount 返回可用 CPU 核数。
func CPUCount() int { return runtime.NumCPU() }