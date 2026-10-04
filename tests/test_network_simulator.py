"""
网络延迟与丢包模拟系统 - 验收测试
运行方式：python -m pytest tests/test_network_simulator.py -q
共 14 个测试用例
"""
import pytest
import sys
import os
import statistics

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from network.network_simulator import NetworkSimulator


def make_sim(uplink=None, downlink=None, seed=42):
    config = {}
    if uplink is not None:
        config["uplink"] = uplink
    if downlink is not None:
        config["downlink"] = downlink
    return NetworkSimulator(config=config, seed=seed)


def run_for(sim, seconds, step=0.01):
    elapsed = 0.0
    while elapsed < seconds - 1e-12:
        sim.update(step)
        elapsed += step


class TestNetworkSimulator:
    def setup_method(self):
        self.system = NetworkSimulator()

    # ---- 延迟模拟 ----
    def test_case_01(self):
        """延迟实际生效：500ms 延迟的包在 400ms 时不可达，600ms 时到达"""
        sim = make_sim(uplink={"latency_ms": 500})
        sim.send_packet("hello", direction="uplink")
        run_for(sim, 0.4)
        assert sim.receive("uplink") == []
        run_for(sim, 0.2)
        received = sim.receive("uplink")
        assert len(received) == 1
        assert received[0].data == "hello"

    def test_case_02(self):
        """延迟区分上下行：上行 500ms、下行 20ms 互不影响"""
        sim = make_sim(uplink={"latency_ms": 500}, downlink={"latency_ms": 20})
        sim.send_packet("up", direction="uplink")
        sim.send_packet("down", direction="downlink")
        run_for(sim, 0.1)
        assert len(sim.receive("downlink")) == 1
        assert sim.receive("uplink") == []
        run_for(sim, 0.5)
        assert len(sim.receive("uplink")) == 1

    def test_case_03(self):
        """延迟有抖动：延迟不再是固定值"""
        sim = make_sim(uplink={"latency_ms": 100, "jitter_min_ms": 10,
                               "jitter_max_ms": 50})
        for _ in range(200):
            sim.send_packet("p", direction="uplink")
        run_for(sim, 1.0)
        latencies = sim.get_stats("uplink").latencies
        assert len(latencies) == 200
        assert max(latencies) - min(latencies) > 5  # 存在波动

    # ---- 丢包模拟 ----
    def test_case_04(self):
        """丢包实际生效：30% 丢包率下确实丢包且比例接近 30%"""
        sim = make_sim(uplink={"loss_rate": 0.3})
        for _ in range(1000):
            sim.send_packet("p", direction="uplink")
        run_for(sim, 0.1)
        stats = sim.get_stats("uplink")
        assert 200 < stats.dropped_loss < 400
        assert stats.delivered + stats.dropped_loss == stats.sent

    def test_case_05(self):
        """丢包区分上下行：上行 50% 丢包，下行 0% 丢包"""
        sim = make_sim(uplink={"loss_rate": 0.5}, downlink={"loss_rate": 0.0})
        for _ in range(1000):
            sim.send_packet("p", direction="uplink")
            sim.send_packet("p", direction="downlink")
        run_for(sim, 0.1)
        up = sim.get_stats("uplink")
        down = sim.get_stats("downlink")
        assert 400 < up.dropped_loss < 600
        assert down.dropped_loss == 0
        assert down.delivered == 1000

    def test_case_06(self):
        """丢包有模式：突发模式产生连续丢包"""
        sim = make_sim(uplink={"loss_rate": 0.3, "loss_mode": "burst",
                               "burst_length": 5})
        results = [sim.send_packet("p", direction="uplink") for _ in range(500)]
        longest_run, run = 0, 0
        for r in results:
            run = run + 1 if r is None else 0
            longest_run = max(longest_run, run)
        assert longest_run >= 3  # 存在连续丢包突发
        assert 0 < sum(1 for r in results if r is None) < 500

    # ---- 带宽限制 ----
    def test_case_07(self):
        """带宽限制实际生效：1Mbps 传 1Mbit 约需 1 秒"""
        sim = make_sim(downlink={"bandwidth_mbps": 1})
        for _ in range(100):  # 100 x 1250B = 1Mbit
            sim.send_packet(b"x" * 1250, direction="downlink")
        run_for(sim, 0.5)
        mid = sim.get_stats("downlink").delivered
        assert 0 < mid < 80  # 半秒时远未传完
        run_for(sim, 1.5)
        assert sim.get_stats("downlink").delivered == 100

    def test_case_08(self):
        """带宽区分上下行：上行限速 1Mbps，下行不限"""
        sim = make_sim(uplink={"bandwidth_mbps": 1},
                       downlink={"bandwidth_mbps": 0})
        for _ in range(100):
            sim.send_packet(b"x" * 1250, direction="uplink")
            sim.send_packet(b"x" * 1250, direction="downlink")
        run_for(sim, 0.1)
        assert sim.get_stats("downlink").delivered == 100
        assert sim.get_stats("uplink").delivered < 100

    def test_case_09(self):
        """带宽有队列：超限数据包排队等待而不是直接丢弃"""
        sim = make_sim(downlink={"bandwidth_mbps": 1})
        for _ in range(10):
            assert sim.send_packet(b"x" * 1250, direction="downlink") is not None
        run_for(sim, 2.0)
        stats = sim.get_stats("downlink")
        assert stats.dropped_queue == 0
        assert stats.delivered == 10

    # ---- 抖动模拟 ----
    def test_case_10(self):
        """抖动有范围：延迟落在 [min, max] 之间"""
        sim = make_sim(uplink={"latency_ms": 0, "jitter_min_ms": 20,
                               "jitter_max_ms": 40})
        for _ in range(500):
            sim.send_packet("p", direction="uplink")
        run_for(sim, 1.0)
        latencies = sim.get_stats("uplink").latencies
        assert all(20 - 1e-6 <= lat <= 40 + 1e-6 for lat in latencies)

    def test_case_11(self):
        """抖动有分布：均匀分布与正态分布的离散程度不同"""
        def collect(distribution):
            sim = make_sim(uplink={"latency_ms": 0, "jitter_min_ms": 0,
                                   "jitter_max_ms": 60,
                                   "jitter_distribution": distribution})
            for _ in range(2000):
                sim.send_packet("p", direction="uplink")
            run_for(sim, 1.0)
            return sim.get_stats("uplink").latencies

        uniform_std = statistics.pstdev(collect("uniform"))
        normal_std = statistics.pstdev(collect("normal"))
        assert uniform_std > 14  # 均匀分布 std ≈ 17.3
        assert normal_std < 14   # 正态分布 std ≈ 10

    # ---- 网络配置 ----
    def test_case_12(self):
        """配置支持自定义：创建自定义配置并生效"""
        sim = NetworkSimulator(seed=1)
        sim.create_profile("my_net", {
            "uplink": {"latency_ms": 300, "loss_rate": 0.1},
            "downlink": {"latency_ms": 100},
        })
        assert "my_net" in sim.list_profiles()
        sim.switch_profile("my_net")
        assert sim.current_profile == "my_net"
        sim.send_packet("p", direction="uplink")
        run_for(sim, 0.2)
        assert sim.receive("uplink") == []
        run_for(sim, 0.2)
        assert len(sim.receive("uplink")) == 1
        with pytest.raises(KeyError):
            sim.switch_profile("not_exist")

    def test_case_13(self, tmp_path):
        """配置能保存：保存到文件后可在新实例中加载"""
        path = str(tmp_path / "profiles.json")
        sim = NetworkSimulator(seed=1)
        sim.create_profile("saved_net", {
            "uplink": {"latency_ms": 250},
            "downlink": {"bandwidth_mbps": 5},
        })
        sim.switch_profile("saved_net")
        sim.save_profiles(path)

        sim2 = NetworkSimulator(seed=1)
        sim2.load_profiles(path)
        assert "saved_net" in sim2.list_profiles()
        assert sim2.current_profile == "saved_net"
        sim2.send_packet("p", direction="uplink")
        run_for(sim2, 0.2)
        assert sim2.receive("uplink") == []
        run_for(sim2, 0.1)
        assert len(sim2.receive("uplink")) == 1

    def test_case_14(self):
        """配置能切换：游戏中切换网络配置立即生效"""
        sim = NetworkSimulator(seed=1)
        sim.create_profile("laggy", {
            "uplink": {"latency_ms": 500},
            "downlink": {"latency_ms": 500},
        })
        sim.send_packet("fast", direction="uplink")
        run_for(sim, 0.05)
        assert len(sim.receive("uplink")) == 1  # normal 配置下几乎无延迟

        sim.switch_profile("laggy")  # 游戏中切换到弱网
        sim.send_packet("slow", direction="uplink")
        run_for(sim, 0.2)
        assert sim.receive("uplink") == []
        run_for(sim, 0.4)
        assert len(sim.receive("uplink")) == 1

        sim.switch_profile("normal")  # 切回正常网络
        sim.send_packet("fast2", direction="uplink")
        run_for(sim, 0.05)
        assert len(sim.receive("uplink")) == 1


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
