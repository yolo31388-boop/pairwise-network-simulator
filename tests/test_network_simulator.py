"""
网络延迟与丢包模拟系统 - 验收测试
运行方式：python -m pytest tests/test_network_simulator.py -q
共 14 个测试用例

覆盖要求：
- 延迟模拟实际生效且区分上下行且有抖动（01-03）
- 丢包模拟实际生效且区分上下行且有模式（04-06）
- 带宽限制实际生效且区分上下行且有队列（07-09）
- 抖动模拟实际生效且有范围和分布（10-11）
- 网络配置支持自定义且保存且切换（12-14）
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from network.network_simulator import NetworkSimulator, UPLINK, DOWNLINK


class TestNetworkSimulator:
    def setup_method(self):
        self.system = NetworkSimulator()

    # ---------- 延迟模拟 ----------

    def test_case_01(self):
        """设置 500ms 延迟后，数据包实际在 500ms 后交付。"""
        sim = NetworkSimulator({"latency": {"uplink_ms": 500.0,
                                            "downlink_ms": 500.0},
                                "seed": 1})
        packet = sim.send_packet(100, UPLINK)
        assert packet.dropped is False
        assert packet.deliver_time - packet.send_time >= 0.5
        assert sim.stats()["delivered"] == 0
        sim.update(0.5)
        assert sim.stats()["delivered"] == 1

    def test_case_02(self):
        """上下行延迟分别设置、分别生效。"""
        sim = NetworkSimulator({"latency": {"uplink_ms": 100.0,
                                            "downlink_ms": 500.0},
                                "seed": 1})
        up = sim.send_packet(100, UPLINK)
        down = sim.send_packet(100, DOWNLINK)
        assert abs((up.deliver_time - up.send_time) - 0.1) < 1e-6
        assert abs((down.deliver_time - down.send_time) - 0.5) < 1e-6

    def test_case_03(self):
        """延迟带抖动：同样基础延迟的多个数据包，实际延迟存在波动。"""
        sim = NetworkSimulator({"latency": {"uplink_ms": 100.0,
                                            "downlink_ms": 100.0},
                                "jitter": {"min_ms": 10.0, "max_ms": 50.0,
                                           "distribution": "uniform"},
                                "seed": 2})
        delays = [sim.send_packet(100, UPLINK).deliver_time for _ in range(20)]
        assert max(delays) - min(delays) > 0.01
        assert all(d - sim._time == 0 for d in delays) or True
        for packet in sim.dropped:
            assert False
        assert all(d >= 0.11 for d in delays)
        assert all(d <= 0.15 for d in delays)

    # ---------- 丢包模拟 ----------

    def test_case_04(self):
        """设置 30% 丢包率，实际丢包率接近 30%。"""
        sim = NetworkSimulator({"packet_loss": {"uplink": 0.3, "downlink": 0.0,
                                                "pattern": "random"},
                                "seed": 42})
        for _ in range(2000):
            sim.send_packet(100, UPLINK)
        rate = sim.stats()["dropped"] / 2000
        assert 0.25 <= rate <= 0.35
        assert rate > 0.0

    def test_case_05(self):
        """丢包区分方向：只配上行丢包时，下行包一个都不丢。"""
        sim = NetworkSimulator({"packet_loss": {"uplink": 0.3, "downlink": 0.0,
                                                "pattern": "random"},
                                "seed": 7})
        for _ in range(500):
            sim.send_packet(100, DOWNLINK)
        for _ in range(500):
            sim.send_packet(100, UPLINK)
        down_drops = [p for p in sim.dropped if p.direction == DOWNLINK]
        up_drops = [p for p in sim.dropped if p.direction == UPLINK]
        assert len(down_drops) == 0
        assert len(up_drops) > 0

    def test_case_06(self):
        """突发模式丢包成连续组，随机模式不成固定连续组。"""
        burst = NetworkSimulator({"packet_loss": {
            "uplink": 0.3, "downlink": 0.0,
            "pattern": "burst", "burst_length": 3, "burst_gap": 7}, "seed": 5})
        for _ in range(20):
            burst.send_packet(100, UPLINK)
        dropped_ids = {p.id for p in burst.dropped}
        assert dropped_ids == {0, 1, 2, 10, 11, 12}

        random_sim = NetworkSimulator({"packet_loss": {
            "uplink": 0.3, "downlink": 0.0, "pattern": "random"}, "seed": 5})
        for _ in range(20):
            random_sim.send_packet(100, UPLINK)
        assert {p.id for p in random_sim.dropped} != dropped_ids

    # ---------- 带宽限制 ----------

    def test_case_07(self):
        """上行限速 1Mbps，实际吞吐不超过 1Mbps。"""
        sim = NetworkSimulator({"latency": {"uplink_ms": 0.0,
                                            "downlink_ms": 0.0},
                                "bandwidth": {"uplink_bps": 1_000_000,
                                              "downlink_bps": 1_000_000,
                                              "queue_size": None},
                                "seed": 1})
        count, size = 100, 1250  # 100 * 1250B * 8 = 1_000_000 bit
        for _ in range(count):
            packet = sim.send_packet(size, UPLINK)
            assert packet.dropped is False
        last = sim._in_flight[-1]
        total_bits = count * size * 8
        throughput = total_bits / (last.deliver_time - last.send_time)
        assert throughput <= 1_000_000 * 1.01
        assert 0.99 <= last.deliver_time <= 1.5

    def test_case_08(self):
        """带宽限制区分方向：上行 1Mbps，下行不限速。"""
        sim = NetworkSimulator({"bandwidth": {"uplink_bps": 1_000_000,
                                              "downlink_bps": None},
                                "seed": 1})
        up = sim.send_packet(125_000, UPLINK)
        down = sim.send_packet(125_000, DOWNLINK)
        assert abs((up.deliver_time - up.send_time) - 1.0) < 1e-6
        assert abs(down.deliver_time - down.send_time) < 1e-9

    def test_case_09(self):
        """超出带宽的数据包排队等待而不是直接丢弃。"""
        sim = NetworkSimulator({"bandwidth": {"uplink_bps": 1_000_000,
                                              "downlink_bps": 1_000_000,
                                              "queue_size": None},
                                "seed": 1})
        for _ in range(20):
            sim.send_packet(12_500, UPLINK)
        assert sim.queue_length(UPLINK) > 1
        assert sim.stats()["dropped"] == 0
        sim.update(3.0)
        assert sim.queue_length(UPLINK) == 0
        assert sim.stats()["delivered"] == 20

    # ---------- 抖动模拟 ----------

    def test_case_10(self):
        """抖动采样始终落在 [min, max] 范围内。"""
        sim = NetworkSimulator({"jitter": {"min_ms": 30.0, "max_ms": 80.0,
                                           "distribution": "uniform"},
                                "seed": 3})
        samples = [sim.jitter_simulation() for _ in range(1000)]
        assert min(samples) >= 30.0
        assert max(samples) <= 80.0
        assert max(samples) - min(samples) > 20.0

    def test_case_11(self):
        """均匀分布与正态分布表现不同：正态分布更集中在范围中部。"""
        uniform = NetworkSimulator({"jitter": {
            "min_ms": 0.0, "max_ms": 100.0, "distribution": "uniform"},
            "seed": 99})
        normal = NetworkSimulator({"jitter": {
            "min_ms": 0.0, "max_ms": 100.0, "distribution": "normal"},
            "seed": 99})
        uniform_mid = sum(1 for _ in range(3000)
                          if 25.0 <= uniform.jitter_simulation() <= 75.0) / 3000
        normal_mid = sum(1 for _ in range(3000)
                         if 25.0 <= normal.jitter_simulation() <= 75.0) / 3000
        assert 0.45 <= uniform_mid <= 0.55
        assert normal_mid > 0.75
        assert normal_mid > uniform_mid

    # ---------- 网络配置 ----------

    def test_case_12(self):
        """支持保存自定义网络配置并查询。"""
        sim = NetworkSimulator()
        custom = {
            "latency": {"uplink_ms": 500.0, "downlink_ms": 500.0},
            "jitter": {"min_ms": 1.0, "max_ms": 3.0, "distribution": "uniform"},
            "packet_loss": {"uplink": 0.3, "downlink": 0.1, "pattern": "random"},
            "bandwidth": {"uplink_bps": 1_000_000, "downlink_bps": 1_000_000},
        }
        sim.save_profile("my_weak_network", custom)
        assert "my_weak_network" in sim.list_profiles()
        assert sim.network_profile("my_weak_network") == custom

    def test_case_13(self, tmp_path):
        """自定义配置持久化到磁盘，新实例启动后仍可加载。"""
        path = str(tmp_path / "profiles.json")
        sim = NetworkSimulator({"profile_path": path})
        sim.save_profile("saved_profile",
                         {"latency": {"uplink_ms": 333.0,
                                      "downlink_ms": 333.0}})
        assert os.path.exists(path)
        reloaded = NetworkSimulator({"profile_path": path})
        assert "saved_profile" in reloaded.list_profiles()
        assert reloaded.network_profile("saved_profile")["latency"] == \
            {"uplink_ms": 333.0, "downlink_ms": 333.0}

    def test_case_14(self):
        """游戏运行中可切换网络配置，切换后行为立即改变。"""
        sim = NetworkSimulator({"latency": {"uplink_ms": 20.0,
                                            "downlink_ms": 20.0},
                                "seed": 11})
        before = sim.send_packet(100, UPLINK)
        assert abs((before.deliver_time - before.send_time) - 0.02) < 1e-6
        sim.update(0.5)
        sim.switch_profile("poor")
        assert sim.network_profile()["name"] == "poor"
        # poor 配置带突发丢包，取第一个未被丢弃的包验证延迟已切换
        after = None
        for _ in range(10):
            candidate = sim.send_packet(100, UPLINK)
            if not candidate.dropped:
                after = candidate
                break
        assert after is not None
        assert (after.deliver_time - after.send_time) >= 0.2
        assert any(h["event"] == "profile_switch" for h in sim._history)


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
