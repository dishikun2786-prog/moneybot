//go:build windows

package ipc

// Open Windows 开发桩：生产环境用 Linux mmap；此处用内存缓冲验证布局逻辑。
func Open(path string) (*Shm, error) {
	return &Shm{data: make([]byte, TotalSize)}, nil
}

// Close no-op。
func (s *Shm) Close() error { return nil }