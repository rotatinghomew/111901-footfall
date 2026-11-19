"""自造小资料的工具：测试用，不依赖真实 data 目录的具体数值。"""

from __future__ import annotations

from pathlib import Path

from footfall.data import Dataset, Edge, Incident, Policy, Reading, Topology, Zone


def make_dataset(
    *,
    zones: list[tuple[str, int, int, int]] | None = None,
    edges: list[tuple[str, str, str, int]] | None = None,
    readings: list[tuple[str, str, str, int, int, str]] | None = None,
    incidents: list[tuple[str, str, int | None, tuple[str, ...], bool]] | None = None,
    policy: Policy | None = None,
) -> Dataset:
    """用元组构造资料。

    zones: ``(name, capacity, door_limit, initial_count)``
    edges: ``(name, from, to, max_flow)``
    readings: ``(device, zone, window, cumulative, reset, kind)``
    incidents: ``(window, zone, manual_count, closed_edges, confirmed)``
    """

    zone_specs = zones or [
        ("lobby", 40, 20, 4),
        ("hall", 20, 10, 2),
    ]
    edge_specs = edges if edges is not None else [("e1", "lobby", "hall", 10)]
    topology = Topology.build(
        [Zone(name, cap, door, init) for name, cap, door, init in zone_specs],
        [Edge(name, a, b, flow) for name, a, b, flow in edge_specs],
    )
    loaded_readings = [
        Reading(device, zone, window, cumulative, bool(reset), kind)
        for device, zone, window, cumulative, reset, kind in (readings or [])
    ]
    loaded_incidents = [
        Incident(window, zone, manual, tuple(closed), bool(confirmed))
        for window, zone, manual, closed, confirmed in (incidents or [])
    ]
    return Dataset(topology, loaded_readings, loaded_incidents, policy or Policy())


def write_csv_directory(
    directory: Path,
    *,
    zones: list[tuple[str, int, int, int]],
    edges: list[tuple[str, str, str, int]],
    readings: list[tuple[str, str, str, int, int, str]],
    incidents: list[tuple[str, str, int | None, tuple[str, ...], bool]],
) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "zones.csv").write_text(
        "zone,capacity,door_limit,initial_count\n"
        + "".join(f"{z},{cap},{door},{init}\n" for z, cap, door, init in zones),
        encoding="utf-8",
    )
    (directory / "edges.csv").write_text(
        "edge,from,to,max_flow\n"
        + "".join(f"{name},{a},{b},{flow}\n" for name, a, b, flow in edges),
        encoding="utf-8",
    )
    (directory / "readings.csv").write_text(
        "device,zone,window,cumulative,reset,kind\n"
        + "".join(
            f"{device},{zone},{window},{cumulative},{reset},{kind}\n"
            for device, zone, window, cumulative, reset, kind in readings
        ),
        encoding="utf-8",
    )
    (directory / "incidents.csv").write_text(
        "window,zone,manual_count,closed_edges,confirmed\n"
        + "".join(
            f"{window},{zone},{'' if manual is None else manual},"
            f"{';'.join(closed)},{1 if confirmed else 0}\n"
            for window, zone, manual, closed, confirmed in incidents
        ),
        encoding="utf-8",
    )
    return directory
