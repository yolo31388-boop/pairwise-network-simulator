"""
网络延迟与丢包模拟系统 - 核心模块

功能：
- 延迟模拟：实际延迟数据包，区分上下行，支持抖动
- 丢包模拟：实际丢包，区分上下行，支持随机/突发两种模式
- 带宽限制：实际限速，区分上下行，超出带宽的数据包排队等待
- 抖动模拟：支持范围（min/max）与分布（均匀/正态）
- 网络配置：预设 + 自定义，支持保存到磁盘与运行时切换
"""
from collections import deque
from dataclasses import dataclass, field
from typing import Deque, Dict, List, Optional
import copy
import json
import os
import random

UPLINK = "uplink"
DOWNLINK = "downlink"
DIRECTIONS = (UPLINK, DOWNLINK)


@dataclass
class Config:
    pass


@dataclass
class Packet:
    id: int
    size: int  # 字节
    direction: str
    send_time: float
    deliver_time: Optional[float] = None
    dropped: bool = False
    drop_reason: Optional[str] = None
    latency_ms: float = 0.0
    jitter_ms: float = 0.0


DEFAULT_CONFIG: Dict = {
    "latency": {"uplink_ms": 0.0, "downlink_ms": 0.0},
    "jitter": {"min_ms": 0.0, "max_ms": 0.0, "distribution": "uniform"},
    "packet_loss": {
        "uplink": 0.0,
        "downlink": 0.0,
        "pattern": "random",  # random | burst
        "burst_length": 3,
        "burst_gap": None,  # None 时按丢包率自动推导
    },
    "bandwidth": {
        "uplink_bps": None,   # None 表示不限速
        "downlink_bps": None,
        "queue_size": None,   # None 表示队列无限长（排队而不是丢弃）
    },
    "seed": None,
    "profile_path": None,
}

DEFAULT_PROFILES: Dict[str, Dict] = {
    "good": {
        "latency": {"uplink_ms": 20.0, "downlink_ms": 20.0},
        "jitter": {"min_ms": 0.0, "max_ms": 5.0, "distribution": "uniform"},
        "packet_loss": {"uplink": 0.0, "downlink": 0.0, "pattern": "random"},
        "bandwidth": {"uplink_bps": None, "downlink_bps": None},
    },
    "average": {
        "latency": {"uplink_ms": 60.0, "downlink_ms": 80.0},
        "jitter": {"min_ms": 5.0, "max_ms": 20.0, "distribution": "uniform"},
        "packet_loss": {"uplink": 0.01, "downlink": 0.01, "pattern": "random"},
        "bandwidth": {"uplink_bps": 10_000_000, "downlink_bps": 50_000_000},
    },
    "poor": {
        "latency": {"uplink_ms": 200.0, "downlink_ms": 300.0},
        "jitter": {"min_ms": 20.0, "max_ms": 100.0, "distribution": "normal"},
        "packet_loss": {"uplink": 0.05, "downlink": 0.08, "pattern": "burst",
                        "burst_length": 3},
        "bandwidth": {"uplink_bps": 1_000_000, "downlink_bps": 2_000_000},
    },
    "3g": {
        "latency": {"uplink_ms": 100.0, "downlink_ms": 150.0},
        "jitter": {"min_ms": 10.0, "max_ms": 60.0, "distribution": "normal"},
        "packet_loss": {"uplink": 0.02, "downlink": 0.02, "pattern": "random"},
        "bandwidth": {"uplink_bps": 1_000_000, "downlink_bps": 3_000_000},
    },
    "4g": {
        "latency": {"uplink_ms": 40.0, "downlink_ms": 50.0},
        "jitter": {"min_ms": 2.0, "max_ms": 15.0, "distribution": "uniform"},
        "packet_loss": {"uplink": 0.005, "downlink": 0.005, "pattern": "random"},
        "bandwidth": {"uplink_bps": 20_000_000, "downlink_bps": 100_000_000},
    },
}

_DEFAULT_PROFILE_PATH = os.path.join(
    os.path.expanduser("~"), ".network_simulator_profiles.json"
)


def _deep_merge(base: Dict, override: Dict) -> Dict:
    merged = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


class NetworkSimulator:
    def __init__(self, config: Optional[Dict] = None):
        config = config or {}
        self._cfg = _deep_merge(DEFAULT_CONFIG, config)
        self.config = self._cfg
        self._rng = random.Random(self._cfg.get("seed"))
        self._profile_path = self._cfg.get("profile_path") or _DEFAULT_PROFILE_PATH
        self._profiles: Dict[str, Dict] = copy.deepcopy(DEFAULT_PROFILES)
        self._profiles.update(self._load_profiles_from_disk())
        self._active_profile: Optional[str] = None
        self._state: Dict = {}
        self._history: List[Dict] = []
        self._time = 0.0
        self._next_id = 0
        self._in_flight: List[Packet] = []
        self._delivered: List[Packet] = []
        self._dropped: List[Packet] = []
        self._queues: Dict[str, Deque[Packet]] = {d: deque() for d in DIRECTIONS}
        self._link_free_time: Dict[str, float] = {d: 0.0 for d in DIRECTIONS}
        self._loss_counters: Dict[str, int] = {d: 0 for d in DIRECTIONS}

    # ------------------------------------------------------------------
    # 延迟与抖动
    # ------------------------------------------------------------------
    def jitter_simulation(self) -> float:
        """按配置的范围与分布采样一次抖动（毫秒）。"""
        jitter_cfg = self._cfg["jitter"]
        lo = max(0.0, float(jitter_cfg.get("min_ms", 0.0)))
        hi = max(lo, float(jitter_cfg.get("max_ms", 0.0)))
        if hi == lo:
            return lo
        distribution = jitter_cfg.get("distribution", "uniform")
        if distribution == "normal":
            mean = (lo + hi) / 2.0
            stddev = (hi - lo) / 6.0  # 99.7% 落在范围内
            value = self._rng.gauss(mean, stddev)
            return min(max(value, lo), hi)
        return self._rng.uniform(lo, hi)

    def simulate_latency(self, direction: str = UPLINK) -> float:
        """返回该方向本次数据包实际承受的延迟（基础延迟 + 抖动，毫秒）。"""
        self._check_direction(direction)
        base = float(self._cfg["latency"].get(f"{direction}_ms", 0.0))
        return base + self.jitter_simulation()

    # ------------------------------------------------------------------
    # 丢包
    # ------------------------------------------------------------------
    def simulate_packet_loss(self, direction: str = UPLINK) -> bool:
        """判定该方向当前数据包是否丢失，支持 random / burst 两种模式。"""
        self._check_direction(direction)
        loss_cfg = self._cfg["packet_loss"]
        rate = float(loss_cfg.get(direction, 0.0))
        pattern = loss_cfg.get("pattern", "random")
        if pattern == "burst":
            if rate <= 0.0:
                return False
            if rate >= 1.0:
                return True
            burst_length = max(1, int(loss_cfg.get("burst_length", 3)))
            gap = loss_cfg.get("burst_gap")
            if gap is None:
                gap = max(1, round(burst_length * (1.0 - rate) / rate))
            cycle = burst_length + int(gap)
            position = self._loss_counters[direction] % cycle
            self._loss_counters[direction] += 1
            return position < burst_length
        if rate <= 0.0:
            return False
        if rate >= 1.0:
            return True
        return self._rng.random() < rate

    # ------------------------------------------------------------------
    # 带宽限制（带队列）
    # ------------------------------------------------------------------
    def bandwidth_limit(self, packet: Packet) -> Packet:
        """按方向带宽为数据包排队调度，计算实际交付时间。

        超出带宽的数据包进入队列等待；仅当队列长度超过 queue_size
        （且 queue_size 被显式设置）时才会发生队尾丢弃。
        """
        bw_cfg = self._cfg["bandwidth"]
        direction = packet.direction
        propagation = (packet.latency_ms + packet.jitter_ms) / 1000.0
        bps = bw_cfg.get(f"{direction}_bps")
        if not bps:
            packet.deliver_time = self._time + propagation
            return packet
        queue = self._queues[direction]
        queue_size = bw_cfg.get("queue_size")
        if queue_size is not None and len(queue) >= int(queue_size):
            packet.dropped = True
            packet.drop_reason = "queue_overflow"
            return packet
        start = max(self._time, self._link_free_time[direction])
        serialization = packet.size * 8.0 / float(bps)
        self._link_free_time[direction] = start + serialization
        packet.deliver_time = start + serialization + propagation
        queue.append(packet)
        return packet

    # ------------------------------------------------------------------
    # 数据包收发管线
    # ------------------------------------------------------------------
    def send_packet(self, size: int, direction: str = UPLINK) -> Packet:
        """发送一个数据包，依次经过丢包、延迟+抖动、带宽队列。"""
        self._check_direction(direction)
        packet = Packet(
            id=self._next_id, size=int(size), direction=direction,
            send_time=self._time,
        )
        self._next_id += 1
        if self.simulate_packet_loss(direction):
            packet.dropped = True
            packet.drop_reason = "packet_loss"
            self._dropped.append(packet)
            self._record("drop", packet)
            return packet
        latency_cfg = self._cfg["latency"]
        packet.latency_ms = float(latency_cfg.get(f"{direction}_ms", 0.0))
        packet.jitter_ms = self.jitter_simulation()
        self.bandwidth_limit(packet)
        if packet.dropped:
            self._dropped.append(packet)
            self._record("drop", packet)
        else:
            self._in_flight.append(packet)
            self._record("send", packet)
        return packet

    def update(self, dt: float):
        """推进模拟时钟，交付到期的数据包。"""
        self._time += float(dt)
        still_in_flight = []
        for packet in self._in_flight:
            if packet.deliver_time is not None and packet.deliver_time <= self._time:
                self._delivered.append(packet)
                self._record("deliver", packet)
            else:
                still_in_flight.append(packet)
        self._in_flight = still_in_flight
        for direction in DIRECTIONS:
            queue = self._queues[direction]
            while queue and queue[0].deliver_time is not None \
                    and queue[0].deliver_time <= self._time:
                queue.popleft()

    def reset(self):
        """清空运行时状态（保留配置与已保存的配置文件）。"""
        self._state = {}
        self._history = []
        self._time = 0.0
        self._next_id = 0
        self._in_flight = []
        self._delivered = []
        self._dropped = []
        self._queues = {d: deque() for d in DIRECTIONS}
        self._link_free_time = {d: 0.0 for d in DIRECTIONS}
        self._loss_counters = {d: 0 for d in DIRECTIONS}

    # ------------------------------------------------------------------
    # 网络配置（profile）
    # ------------------------------------------------------------------
    def network_profile(self, name: Optional[str] = None) -> Dict:
        """查询网络配置。name 为 None 时返回当前生效配置。"""
        if name is None:
            profile = copy.deepcopy(self._cfg)
            profile["name"] = self._active_profile
            return profile
        if name not in self._profiles:
            raise KeyError(f"未知的网络配置: {name}")
        return copy.deepcopy(self._profiles[name])

    def list_profiles(self) -> List[str]:
        return sorted(self._profiles.keys())

    def save_profile(self, name: str, config: Dict):
        """保存自定义配置（内存 + 磁盘持久化）。"""
        if not name:
            raise ValueError("配置名称不能为空")
        if not isinstance(config, dict) or not config:
            raise ValueError("配置内容不能为空")
        self._profiles[name] = copy.deepcopy(config)
        self._persist_profiles()

    def switch_profile(self, name: str):
        """运行时切换网络配置（游戏中可调用）。"""
        if name not in self._profiles:
            raise KeyError(f"未知的网络配置: {name}")
        self._cfg = _deep_merge(self._cfg, self._profiles[name])
        self.config = self._cfg
        self._active_profile = name
        # 链路参数变化后重新计价，避免沿用旧带宽的排程结果
        self._link_free_time = {d: self._time for d in DIRECTIONS}
        self._loss_counters = {d: 0 for d in DIRECTIONS}
        self._history.append({"event": "profile_switch", "name": name,
                              "time": self._time})

    apply_profile = switch_profile

    # ------------------------------------------------------------------
    # 状态查询
    # ------------------------------------------------------------------
    @property
    def delivered(self) -> List[Packet]:
        return list(self._delivered)

    @property
    def dropped(self) -> List[Packet]:
        return list(self._dropped)

    def queue_length(self, direction: str = UPLINK) -> int:
        self._check_direction(direction)
        return len(self._queues[direction])

    def stats(self) -> Dict:
        return {
            "time": self._time,
            "sent": self._next_id,
            "delivered": len(self._delivered),
            "dropped": len(self._dropped),
            "in_flight": len(self._in_flight),
            "queued": {d: len(self._queues[d]) for d in DIRECTIONS},
            "active_profile": self._active_profile,
        }

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------
    @staticmethod
    def _check_direction(direction: str):
        if direction not in DIRECTIONS:
            raise ValueError(f"非法方向: {direction}，应为 {DIRECTIONS} 之一")

    def _record(self, event: str, packet: Packet):
        self._history.append({
            "event": event,
            "packet_id": packet.id,
            "direction": packet.direction,
            "time": self._time,
        })

    def _load_profiles_from_disk(self) -> Dict[str, Dict]:
        try:
            with open(self._profile_path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict):
                return data
        except (OSError, ValueError):
            pass
        return {}

    def _persist_profiles(self):
        custom = {
            name: cfg for name, cfg in self._profiles.items()
            if name not in DEFAULT_PROFILES
        }
        try:
            directory = os.path.dirname(self._profile_path)
            if directory:
                os.makedirs(directory, exist_ok=True)
            with open(self._profile_path, "w", encoding="utf-8") as fh:
                json.dump(custom, fh, ensure_ascii=False, indent=2)
        except OSError:
            pass
