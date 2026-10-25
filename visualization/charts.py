"""Plotly figures. Every figure takes DataFrames produced by SQL queries."""
from __future__ import annotations  # modern type hints on older Python

import pandas as pd  # tables
import plotly.express as px  # quick charts
import plotly.graph_objects as go  # detailed charts

from visualization.theme import PALETTE  # shared colours

STRATEGY_COLORS = {"SHORTEST_PATH": "#4da3ff", "CONGESTION_AWARE": "#ffb000", "LOAD_BALANCED": "#2fbf71"}  # one colour per routing strategy


def _style(fig: go.Figure, title: str = "", height: int = 340) -> go.Figure:  # apply the shared look to a chart
    fig.update_layout(title=title, height=height, paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",  # transparent background, title and size
                      font_color=PALETTE["text"], margin=dict(l=10, r=10, t=45, b=10),  # text colour and margins
                      legend=dict(orientation="h", y=-0.2))  # legend below the chart
    fig.update_xaxes(gridcolor=PALETTE["line"])  # subtle vertical grid
    fig.update_yaxes(gridcolor=PALETTE["line"])  # subtle horizontal grid
    return fig  # the styled chart


def occupancy_timeline(df: pd.DataFrame) -> go.Figure:  # people on roads and queued over time
    fig = go.Figure()  # empty chart
    fig.add_scatter(x=df["t"], y=df["agents_on_roads"], name="Agents on roads", line=dict(color=PALETTE["info"]))  # line: people walking
    fig.add_scatter(x=df["t"], y=df["agents_queued_for_edges"], name="Agents queued for an edge",  # line: people waiting (part 1)
                    line=dict(color=PALETTE["warn"], dash="dot"))  # dotted style (part 2)
    fig.update_xaxes(title="Simulation time (s)")  # x-axis label
    fig.update_yaxes(title="Agents")  # y-axis label
    return _style(fig, "Crowd occupancy over time (SQL: edge_occupancy_snapshots)")  # styled chart


def frame_counts(frames: pd.DataFrame) -> go.Figure:  # population by state over time
    fig = go.Figure()  # empty chart
    for col, name, color in (("evacuated_count", "Evacuated", PALETTE["ok"]), ("moving_count", "Moving", PALETTE["info"]),  # states and colours (1)
                             ("queued_count", "Queued", PALETTE["warn"]), ("waiting_count", "Waiting", "#8b97a8"),  # (2)
                             ("failed_count", "Unable", PALETTE["bad"])):  # (3)
        fig.add_scatter(x=frames["simulation_time_seconds"], y=frames[col], name=name, line=dict(color=color))  # one line per state
    fig.update_xaxes(title="Simulation time (s)")  # x-axis label
    return _style(fig, "Population by state")  # styled chart


def histogram(df: pd.DataFrame, col: str, title: str, xlabel: str) -> go.Figure:  # distribution of one column
    fig = px.histogram(df, x=col, nbins=30, color_discrete_sequence=[PALETTE["accent"]])  # 30 bins
    fig.update_xaxes(title=xlabel)  # x-axis label
    fig.update_yaxes(title="Agents")  # y-axis label
    return _style(fig, title)  # styled chart


def bar_by_run(df: pd.DataFrame, col: str, title: str, ytitle: str) -> go.Figure:  # one bar per run
    d = df.copy()  # do not change the input
    d["run"] = "run " + d["run_id"].astype(str) + " " + d["routing_strategy"].str.replace("_", " ").str.title()  # readable label per run
    fig = px.bar(d, x="run", y=col, color="routing_strategy", color_discrete_map=STRATEGY_COLORS)  # coloured by strategy
    fig.update_yaxes(title=ytitle)  # y-axis label
    fig.update_xaxes(title="")  # no x-axis label
    return _style(fig, title)  # styled chart


def peak_ratio_bars(df: pd.DataFrame) -> go.Figure:  # the most congested roads
    d = df.copy()  # do not change the input
    d["edge"] = "edge " + d["edge_id"].astype(str)  # readable road label
    fig = px.bar(d, x="edge", y="peak_ratio", color="peak_queue", color_continuous_scale="OrRd",  # bar height = peak ratio, colour = peak queue (part 1)
                 hover_data=["road_class", "capacity", "snapshots_with_queue"])  # extra details on hover (part 2)
    fig.update_yaxes(title="Peak occupancy / capacity", range=[0, 1.05])  # ratio axis, a little above 1
    return _style(fig, "Most congested road segments (peak occupancy ratio)")  # styled chart


def edge_timeline(df: pd.DataFrame, capacity: int, edge_id: int) -> go.Figure:  # occupancy and queue of one road
    fig = go.Figure()  # empty chart
    fig.add_scatter(x=df["t"], y=df["occupancy"], name="Occupancy", line=dict(color=PALETTE["info"]))  # line: people on the road
    fig.add_scatter(x=df["t"], y=df["queue_length"], name="Queue to enter", line=dict(color=PALETTE["bad"]))  # line: people waiting to enter
    fig.add_hline(y=capacity, line_dash="dash", line_color=PALETTE["muted"], annotation_text="capacity")  # dashed line at the capacity
    fig.update_xaxes(title="Simulation time (s)")  # x-axis label
    return _style(fig, f"Edge {edge_id}: occupancy and queue over time")  # styled chart


def congestion_diff_bars(df: pd.DataFrame) -> go.Figure:  # baseline versus alternative congestion
    d = df.head(15).copy()  # the 15 roads that changed most
    d["edge"] = "edge " + d["edge_id"].astype(str)  # readable road label
    fig = go.Figure()  # empty chart
    fig.add_bar(x=d["edge"], y=d["base_ratio"], name="Baseline peak ratio", marker_color=PALETTE["info"])  # bars: baseline
    fig.add_bar(x=d["edge"], y=d["alt_ratio"], name="Alternative peak ratio", marker_color=PALETTE["accent"])  # bars: alternative
    fig.update_layout(barmode="group")  # side by side
    return _style(fig, "Peak occupancy ratio per segment: baseline vs alternative")  # styled chart
