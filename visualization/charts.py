"""Plotly figures. Every figure takes DataFrames produced by SQL queries."""
from __future__ import annotations

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go

from visualization.theme import PALETTE

STRATEGY_COLORS = {"SHORTEST_PATH": "#4da3ff", "CONGESTION_AWARE": "#ffb000", "LOAD_BALANCED": "#2fbf71"}


def _style(fig: go.Figure, title: str = "", height: int = 340) -> go.Figure:
    fig.update_layout(title=title, height=height, paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
                      font_color=PALETTE["text"], margin=dict(l=10, r=10, t=45, b=10),
                      legend=dict(orientation="h", y=-0.2))
    fig.update_xaxes(gridcolor=PALETTE["line"])
    fig.update_yaxes(gridcolor=PALETTE["line"])
    return fig


def occupancy_timeline(df: pd.DataFrame) -> go.Figure:
    fig = go.Figure()
    fig.add_scatter(x=df["t"], y=df["agents_on_roads"], name="Agents on roads", line=dict(color=PALETTE["info"]))
    fig.add_scatter(x=df["t"], y=df["agents_queued_for_edges"], name="Agents queued for an edge",
                    line=dict(color=PALETTE["warn"], dash="dot"))
    fig.update_xaxes(title="Simulation time (s)")
    fig.update_yaxes(title="Agents")
    return _style(fig, "Crowd occupancy over time (SQL: edge_occupancy_snapshots)")


def frame_counts(frames: pd.DataFrame) -> go.Figure:
    fig = go.Figure()
    for col, name, color in (("evacuated_count", "Evacuated", PALETTE["ok"]), ("moving_count", "Moving", PALETTE["info"]),
                             ("queued_count", "Queued", PALETTE["warn"]), ("waiting_count", "Waiting", "#8b97a8"),
                             ("failed_count", "Unable", PALETTE["bad"])):
        fig.add_scatter(x=frames["simulation_time_seconds"], y=frames[col], name=name, line=dict(color=color))
    fig.update_xaxes(title="Simulation time (s)")
    return _style(fig, "Population by state")


def histogram(df: pd.DataFrame, col: str, title: str, xlabel: str) -> go.Figure:
    fig = px.histogram(df, x=col, nbins=30, color_discrete_sequence=[PALETTE["accent"]])
    fig.update_xaxes(title=xlabel)
    fig.update_yaxes(title="Agents")
    return _style(fig, title)


def bar_by_run(df: pd.DataFrame, col: str, title: str, ytitle: str) -> go.Figure:
    d = df.copy()
    d["run"] = "run " + d["run_id"].astype(str) + " " + d["routing_strategy"].str.replace("_", " ").str.title()
    fig = px.bar(d, x="run", y=col, color="routing_strategy", color_discrete_map=STRATEGY_COLORS)
    fig.update_yaxes(title=ytitle)
    fig.update_xaxes(title="")
    return _style(fig, title)


def peak_ratio_bars(df: pd.DataFrame) -> go.Figure:
    d = df.copy()
    d["edge"] = "edge " + d["edge_id"].astype(str)
    fig = px.bar(d, x="edge", y="peak_ratio", color="peak_queue", color_continuous_scale="OrRd",
                 hover_data=["road_class", "capacity", "snapshots_with_queue"])
    fig.update_yaxes(title="Peak occupancy / capacity", range=[0, 1.05])
    return _style(fig, "Most congested road segments (peak occupancy ratio)")


def edge_timeline(df: pd.DataFrame, capacity: int, edge_id: int) -> go.Figure:
    fig = go.Figure()
    fig.add_scatter(x=df["t"], y=df["occupancy"], name="Occupancy", line=dict(color=PALETTE["info"]))
    fig.add_scatter(x=df["t"], y=df["queue_length"], name="Queue to enter", line=dict(color=PALETTE["bad"]))
    fig.add_hline(y=capacity, line_dash="dash", line_color=PALETTE["muted"], annotation_text="capacity")
    fig.update_xaxes(title="Simulation time (s)")
    return _style(fig, f"Edge {edge_id}: occupancy and queue over time")


def congestion_diff_bars(df: pd.DataFrame) -> go.Figure:
    d = df.head(15).copy()
    d["edge"] = "edge " + d["edge_id"].astype(str)
    fig = go.Figure()
    fig.add_bar(x=d["edge"], y=d["base_ratio"], name="Baseline peak ratio", marker_color=PALETTE["info"])
    fig.add_bar(x=d["edge"], y=d["alt_ratio"], name="Alternative peak ratio", marker_color=PALETTE["accent"])
    fig.update_layout(barmode="group")
    return _style(fig, "Peak occupancy ratio per segment: baseline vs alternative")
