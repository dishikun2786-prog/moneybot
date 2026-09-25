//go:build linux || darwin

package ipc

import (
	"os"
	"syscall"
)

// Open 创建/打开并 mmap 快照文件（MAP_SHARED，fd 关闭后映射仍有效）。
func Open(path string) (*Shm, error) {
	f, err := os.OpenFile(path, os.O_RDWR|os.O_CREATE, 0o644)
	if err != nil {
		return nil, err
	}
	if err := f.Truncate(TotalSize); err != nil {
		_ = f.Close()
		return nil, err
	}
	data, err := syscall.Mmap(int(f.Fd()), 0, TotalSize,
		syscall.PROT_READ|syscall.PROT_WRITE, syscall.MAP_SHARED)
	if err != nil {
		_ = f.Close()
		return nil, err
	}
	_ = f.Close()
	return &Shm{data: data}, nil
}

// Close 解除映射。
func (s *Shm) Close() error { return syscall.Munmap(s.data) }