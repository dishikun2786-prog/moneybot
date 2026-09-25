//go:build linux

package pin

import (
	"fmt"
	"runtime"

	"golang.org/x/sys/unix"
)

// PinToCPU 把当前 goroutine 锁定到指定 CPU 核心。
// 先 runtime.LockOSThread() 把 goroutine 钉在 OS 线程上，再用 sched_setaffinity
// 把该线程钉在指定逻辑核，彻底消除跨核迁移与上下文切换带来的延迟抖动。
// 返回的 unlock 用于解除绑定（应在同一 goroutine 内调用）。
func PinToCPU(cpu int) (func(), error) {
	runtime.LockOSThread()
	var set unix.CPUSet
	set.Zero()
	set.Set(cpu)
	if err := unix.SchedSetaffinity(0, &set); err != nil {
		runtime.UnlockOSThread()
		return nil, fmt.Errorf("pin cpu %d: %w", cpu, err)
	}
	return runtime.UnlockOSThread, nil
}

// RunPinned 在指定 CPU 核心上运行 fn（新 goroutine + 亲和性 + LockOSThread）。
// 典型用法：网络 IO 协程钉核0，FDTD/风控计算协程钉核1..N。
func RunPinned(cpu int, fn func()) error {
	done := make(chan error, 1)
	go func() {
		unlock, err := PinToCPU(cpu)
		if err != nil {
			done <- err
			return
		}
		defer unlock()
		fn()
		done <- nil
	}()
	return <-done
}