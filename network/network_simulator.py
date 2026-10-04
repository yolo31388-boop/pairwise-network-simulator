"""
网络延迟与丢包模拟系统 - 核心模块

功能：
- 延迟模拟：实际延迟数据包，上下行独立，支持抖动
- 丢包模拟：实际丢包，上下行独立，支持随机/突发两种模式
- 带宽限制：令牌桶限速，上下行独立，超限数据包进入队列等待而非丢弃
- 抖动模拟：支持最小/最大范围，支持均匀分布和正态分布
- 网络配置：内置预设 + 自定义配置，支持保存/加载/游戏中切换

模拟采用内部时钟：send_packet() 入队，update(dt) 推进模拟时间，
receive() 取出已到达的数据包。测试无需真实等待。
"""
from dataclasses import dataclass, field, asdict
from typing import Dict, List, Optional, Tuple
import heapq
import json
import math
import random

UPLINK = "uplink"
DOWNLINK = "downlink"
DIRECTIONS = (UPLINK, DOWNLINK)

LOSS_RANDOM = "random"
LOSS_BURST = "burst"

DIST_UNIFORM = "uniform"
DIST_NORMAL = "normal"


@dataclass
class DirectionConfig:
    """单方向（上行或下行）的网络参数。"""
    latency_ms: float = 0.0          # 基础延迟（毫秒）
    jitter_min_ms: float = 0.0       # 抖动最小值（毫秒）
    jitter_max_ms: float = 0.0       # 抖动最大值（毫秒）
    jitter_distribution: str = DIST_UNIFORM  # uniform | normal
    loss_rate: float = 0.0           # 丢包率 0.0 ~ 1.0
    loss_mode: str = LOSS_RANDOM     # random | burst
    burst_length: int = 3            # 突发丢包的连续丢包长度
    bandwidth_mbps: float = 0.0      # 带宽上限（Mbps），0 表示不限
    queue_capacity: int = 1024       # 队列容量（包数），超出才丢弃

    def validate(self) -> None:
        if self.latency_ms < 0:
            raise ValueError("latency_ms 不能为负")
        if self.jitter_min_ms < 0 or self.jitter_max_ms < self.jitter_min_ms:
            raise ValueError("抖动范围非法：需要 0 <= jitter_min_ms <= jitter_max_ms")
        if not 0.0 <= self.loss_rate <= 1.0:
            raise ValueError("loss_rate 必须在 [0, 1] 之间")
        if self.loss_mode not in (LOSS_RANDOM, LOSS_BURST):
            raise ValueError("loss_mode 必须是 random 或 burst")
        if self.jitter_distribution not in (DIST_UNIFORM, DIST_NORMAL):
            raise ValueError("jitter_distribution 必须是 uniform 或 normal")
        if self.bandwidth_mbps < 0:
            raise ValueError("bandwidth_mbps 不能为负")
        if self.burst_length < 1:
            raise ValueError("burst_length 必须 >= 1")


@dataclass
class Packet:
    id: int
    data: object
    size_bytes: int
    direction: str
    sent_at: float = 0.0
    delay_ms: float = 0.0


@dataclass
class DirectionStats:
    sent: int = 0
    delivered: int = 0
    dropped_loss: int = 0
    dropped_queue: int = 0
    bytes_delivered: int = 0
    latencies: List[float] = field(default_factory=list)

    @property
    def loss_rate(self) -> float:
        return self.dropped_loss / self.sent if self.sent else 0.0

    @property
    def avg_latency_ms(self) -> float:
        return sum(self.latencies) / len(self.latencies) if self.latencies else 0.0


# 内置预设网络配置
BUILTIN_PROFILES: Dict[str, Dict] = {
    "normal": {
        "uplink": {},
        "downlink": {},
    },
    "wifi": {
        "uplink": {"latency_ms": 20, "jitter_min_ms": 0, "jitter_max_ms": 10,
                   "loss_rate": 0.01, "bandwidth_mbps": 50},
        "downlink": {"latency_ms": 20, "jitter_min_ms": 0, "jitter_max_ms": 10,
                     "loss_rate": 0.01, "bandwidth_mbps": 100},
    },
    "4g": {
        "uplink": {"latency_ms": 50, "jitter_min_ms": 5, "jitter_max_ms": 30,
                   "loss_rate": 0.02, "bandwidth_mbps": 20},
        "downlink": {"latency_ms": 50, "jitter_min_ms": 5, "jitter_max_ms": 30,
                     "loss_rate": 0.02, "bandwidth_mbps": 100},
    },
    "3g": {
        "uplink": {"latency_ms": 150, "jitter_min_ms": 20, "jitter_max_ms": 100,
                   "loss_rate": 0.05, "bandwidth_mbps": 2},
        "downlink": {"latency_ms": 150, "jitter_min_ms": 20, "jitter_max_ms": 100,
                     "loss_rate": 0.05, "bandwidth_mbps": 8},
    },
    "weak": {
        "uplink": {"latency_ms": 500, "jitter_min_ms": 50, "jitter_max_ms": 200,
                   "loss_rate": 0.3, "loss_mode": "burst", "bandwidth_mbps": 0.5},
        "downlink": {"latency_ms": 500, "jitter_min_ms": 50, "jitter_max_ms": 200,
                     "loss_rate": 0.3, "loss_mode": "burst", "bandwidth_mbps": 1},
    },
}


class NetworkSimulator:
    """网络模拟器：延迟 / 丢包 / 带宽 / 抖动 / 配置管理。"""

    def __init__(self, config: Optional[Dict] = None, seed: Optional[int] = None):
        self._rng = random.Random(seed)
        self._profiles: Dict[str, Dict[str, DirectionConfig]] = {
            name: self._parse_profile(cfg) for name, cfg in BUILTIN_PROFILES.items()
        }
        self._current_profile = "normal"
        if config:
            self._profiles["custom"] = self._parse_profile(config)
            self._current_profile = "custom"
        self._reset_runtime()

    # ------------------------------------------------------------------
    # 配置管理
    # ------------------------------------------------------------------
    @staticmethod
    def _parse_profile(config: Dict) -> Dict[str, DirectionConfig]:
        result = {}
        for direction in DIRECTIONS:
            cfg = DirectionConfig(**(config.get(direction) or {}))
            cfg.validate()
            result[direction] = cfg
        return result

    def create_profile(self, name: str, config: Dict) -> None:
        """创建/覆盖一个自定义网络配置。"""
        if not name:
            raise ValueError("配置名不能为空")
        self._profiles[name] = self._parse_profile(config)

    def switch_profile(self, name: str) -> None:
        """游戏中切换网络配置，立即生效；队列中未处理的包按新参数处理。"""
        if name not in self._profiles:
            raise KeyError(f"网络配置不存在: {name}")
        self._current_profile = name

    def list_profiles(self) -> List[str]:
        return sorted(self._profiles.keys())

    @property
    def current_profile(self) -> str:
        return self._current_profile

    def get_profile(self, name: Optional[str] = None) -> Dict:
        profile = self._profiles[name or self._current_profile]
        return {d: asdict(profile[d]) for d in DIRECTIONS}

    def save_profiles(self, path: str) -> None:
        """把所有配置（含自定义）保存为 JSON 文件。"""
        data = {
            "current": self._current_profile,
            "profiles": {
                name: {d: asdict(cfg[d]) for d in DIRECTIONS}
                for name, cfg in self._profiles.items()
            },
        }
        with open(path, "w", encoding="utf-8") as fp:
            json.dump(data, fp, ensure_ascii=False, indent=2)

    def load_profiles(self, path: str) -> None:
        """从 JSON 文件加载配置并切换到最后使用的配置。"""
        with open(path, "r", encoding="utf-8") as fp:
            data = json.load(fp)
        for name, cfg in data.get("profiles", {}).items():
            self._profiles[name] = self._parse_profile(cfg)
        current = data.get("current")
        if current in self._profiles:
            self._current_profile = current

    # ------------------------------------------------------------------
    # 核心模拟
    # ------------------------------------------------------------------
    def _reset_runtime(self) -> None:
        self._time = 0.0
        self._seq = 0
        self._packet_seq = 0
        self._queues: Dict[str, List[Packet]] = {d: [] for d in DIRECTIONS}
        self._budget: Dict[str, float] = {d: 0.0 for d in DIRECTIONS}
        self._pending: List[Tuple[float, int, Packet]] = []
        self._inbox: Dict[str, List[Packet]] = {d: [] for d in DIRECTIONS}
        self._burst_remaining: Dict[str, int] = {d: 0 for d in DIRECTIONS}
        self._stats: Dict[str, DirectionStats] = {d: DirectionStats() for d in DIRECTIONS}

    def reset(self):
        self._reset_runtime()

    def update(self, dt: float):
        """推进模拟时钟 dt 秒：按带宽放行队列中的包，投递到期的包。"""
        if dt < 0:
            raise ValueError("dt 不能为负")
        self._time += dt
        for direction in DIRECTIONS:
            cfg = self._profiles[self._current_profile][direction]
            if cfg.bandwidth_mbps > 0:
                rate = cfg.bandwidth_mbps * 1_000_000 / 8  # 字节/秒
                # 令牌桶：累积字节预算，上限为 1 秒的流量，防止长时间空闲后瞬时爆发
                self._budget[direction] = min(self._budget[direction] + rate * dt, rate)
                queue = self._queues[direction]
                while queue and queue[0].size_bytes <= self._budget[direction]:
                    packet = queue.pop(0)
                    self._budget[direction] -= packet.size_bytes
                    self._schedule(direction, cfg, packet)
            else:
                queue = self._queues[direction]
                while queue:
                    self._schedule(direction, cfg, queue.pop(0))
        while self._pending and self._pending[0][0] <= self._time + 1e-9:
            _, _, packet = heapq.heappop(self._pending)
            stats = self._stats[packet.direction]
            stats.delivered += 1
            stats.bytes_delivered += packet.size_bytes
            stats.latencies.append(packet.delay_ms)
            self._inbox[packet.direction].append(packet)

    def _schedule(self, direction: str, cfg: DirectionConfig, packet: Packet) -> None:
        delay_ms = cfg.latency_ms + self._sample_jitter(cfg)
        packet.delay_ms = delay_ms
        deliver_at = self._time + delay_ms / 1000.0
        self._seq += 1
        heapq.heappush(self._pending, (deliver_at, self._seq, packet))

    def _sample_jitter(self, cfg: DirectionConfig) -> float:
        lo, hi = cfg.jitter_min_ms, cfg.jitter_max_ms
        if hi <= lo:
            return lo
        if cfg.jitter_distribution == DIST_NORMAL:
            mean = (lo + hi) / 2.0
            std = (hi - lo) / 6.0  # 99.7% 落在范围内
            return min(max(self._rng.gauss(mean, std), lo), hi)
        return self._rng.uniform(lo, hi)

    def _should_drop(self, direction: str, cfg: DirectionConfig) -> bool:
        if cfg.loss_rate <= 0:
            return False
        if cfg.loss_mode == LOSS_BURST:
            if self._burst_remaining[direction] > 0:
                self._burst_remaining[direction] -= 1
                return True
            # 折算进入突发的概率，使整体丢包率等于 loss_rate：
            # 周期 = 平均通过数 (1-p)/p + 突发长度 L，要求 L / (L + (1-p)/p) = loss_rate
            rate, length = cfg.loss_rate, cfg.burst_length
            p_enter = rate / (length - length * rate + rate)
            if self._rng.random() < p_enter:
                self._burst_remaining[direction] = cfg.burst_length - 1
                return True
            return False
        return self._rng.random() < cfg.loss_rate

    def send_packet(self, data: object, direction: str = UPLINK,
                    size_bytes: Optional[int] = None) -> Optional[int]:
        """发送一个数据包。返回包 id；被丢包或队列溢出丢弃时返回 None。"""
        if direction not in DIRECTIONS:
            raise ValueError(f"非法方向: {direction}")
        cfg = self._profiles[self._current_profile][direction]
        stats = self._stats[direction]
        stats.sent += 1
        if self._should_drop(direction, cfg):
            stats.dropped_loss += 1
            return None
        self._packet_seq += 1
        packet = Packet(
            id=self._packet_seq,
            data=data,
            size_bytes=size_bytes if size_bytes is not None else self._estimate_size(data),
            direction=direction,
            sent_at=self._time,
        )
        queue = self._queues[direction]
        if len(queue) >= cfg.queue_capacity:
            stats.dropped_queue += 1
            return None
        queue.append(packet)
        return packet.id

    @staticmethod
    def _estimate_size(data: object) -> int:
        if isinstance(data, (bytes, bytearray)):
            return len(data)
        if isinstance(data, str):
            return len(data.encode("utf-8"))
        return 64

    def receive(self, direction: str = DOWNLINK) -> List[Packet]:
        """取出该方向所有已到达的数据包。"""
        if direction not in DIRECTIONS:
            raise ValueError(f"非法方向: {direction}")
        packets = self._inbox[direction]
        self._inbox[direction] = []
        return packets

    def get_stats(self, direction: Optional[str] = None):
        """查询统计信息；不传方向时返回两个方向的字典。"""
        if direction is None:
            return {d: self._stats[d] for d in DIRECTIONS}
        if direction not in DIRECTIONS:
            raise ValueError(f"非法方向: {direction}")
        return self._stats[direction]

    @property
    def time(self) -> float:
        return self._time
