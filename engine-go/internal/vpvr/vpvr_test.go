package vpvr

import "testing"

func TestPOC(t *testing.T) {
	v := NewVolumeProfile(72, 1.0)
	// 价格 100 附近成交量最大
	for i := 0; i < 100; i++ {
		v.AddTrade(100.1, 1.0)
	}
	for i := 0; i < 30; i++ {
		v.AddTrade(105.0, 1.0)
	}
	for i := 0; i < 20; i++ {
		v.AddTrade(95.0, 1.0)
	}
	poc, ok := v.POC()
	if !ok {
		t.Fatal("POC 应存在")
	}
	if poc < 100 || poc > 101 {
		t.Fatalf("POC 应在 100~101: %v", poc)
	}
}

func TestValueArea(t *testing.T) {
	v := NewVolumeProfile(72, 1.0)
	for i := 0; i < 50; i++ {
		v.AddTrade(100.0, 1.0)
	}
	for i := 0; i < 30; i++ {
		v.AddTrade(102.0, 1.0)
	}
	for i := 0; i < 20; i++ {
		v.AddTrade(98.0, 1.0)
	}
	vah, val, ok := v.ValueArea()
	if !ok {
		t.Fatal("ValueArea 应存在")
	}
	if vah <= val {
		t.Fatalf("VAH %v 应 > VAL %v", vah, val)
	}
	if val < 97 || val > 101 {
		t.Fatalf("VAL 应在 97~101: %v", val)
	}
}

func TestRollHour(t *testing.T) {
	v := NewVolumeProfile(3, 1.0)
	v.AddTrade(100, 10)
	v.RollHour()
	v.AddTrade(100, 10)
	v.RollHour()
	v.AddTrade(100, 10)
	v.RollHour()
	// 3 小时后最旧 1 小时被淘汰，剩 2 小时 = 20
	if v.profile[v.bucket(100)] != 20 {
		t.Fatalf("滑动窗口淘汰后应 20: %v", v.profile[v.bucket(100)])
	}
}
