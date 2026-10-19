# ROS 2 / Gazebo validation — roadmap (NOT IMPLEMENTED)

> **Status: design only.** This repository contains **no ROS 2 code** and **no ROS 2 or Gazebo results**. Nothing here has been built, run or measured. Every statement below about benefits is a *hypothesis to test*, not a finding. The plan follows the optional-extension section of the original project guide ("start only after the simulator-first project passes all exit gates").

## 1. Why a ROS 2 stage at all
The grid simulator proves the *mission layer* (planning validation, allocation, scheduling, replanning, the safety gate) on a discrete abstraction. It cannot show that the same decisions survive **continuous motion, a real navigation stack, asynchronous messaging and timing effects**. A ROS 2 + Gazebo stage would test exactly one question: *does the unchanged mission layer, with the unchanged safety rules, still behave correctly when the robots are driven through ROS 2 interfaces instead of the grid simulator?*

## 2. Planned design (proposed, not existing)
| Element | Plan |
|---|---|
| Mission layer | Unchanged: LLM/mock planner → validators → allocator → coordinator → SafetyMonitor. The LLM still emits task specifications only. |
| Robot boundary | Introduce a `RobotInterface` abstraction (today the coordinator talks to the grid `Simulator` directly — **this refactor does not exist yet**). Implementations: `GridRobotInterface` (current simulator) and `Ros2RobotInterface` (new). |
| Agents | One ROS 2 namespace per agent (`/agent_1/...`), 2–3 robots in a small Gazebo world with static obstacles and reproducible task areas. |
| Interfaces | Typed messages/actions only, never free-form text: a task-command action (task id, allowlisted task type, target zone), task-result, agent-status (position, battery), fault-event. |
| Navigation | Delegated to an established stack (e.g. Nav2) or a controlled robot-specific interface; the mission layer never publishes velocity commands. |
| Hard gate | Every dispatch still passes the deterministic `SafetyMonitor` (capability, dependencies, battery reserve, freshness, allowlist) *before* an action goal is sent. |
| Zone/cell reservation | Grid occupancy rules become a central reservation of task zones/corridors (continuous geometry replaces cell occupancy; **the safety argument must be re-derived, not assumed to carry over**). |
| Logging | Same JSONL event log, plus ROS timestamps and simulation timestamps, QoS settings, and message drops, so existing metric code can be reused. |
| Time/staleness | Messages carry plan/world versions; stale messages are discarded (same rule as the grid monitor). |
| Packaging | Separate package/milestone; the core project must keep running without ROS installed. |

## 3. Staged validation plan
1. **S0 — parity.** One agent, nominal task, no faults: task completes, postcondition checked by the simulator-side executor, event log schema identical to the grid runs.
2. **S1 — multi-agent nominal.** 2–3 agents, scenario s0-equivalent: compare completion, makespan ratio, travel distance against the grid results.
3. **S2 — blocked route.** Inject an obstacle; verify re-routing and that no blocked region is entered after the block event.
4. **S3 — agent failure and replanning.** Equivalent of scenarios s3/s8: failed agent excluded, completed work preserved, remaining work reassigned.
5. **S4 — fault injection in the messaging layer.** Delayed/dropped/stale messages: verify fail-closed behaviour (this would also cover the `COMMUNICATION_DELAY` event type that the grid project does not implement).

Metrics to compare against the grid baseline (same seeds/scenarios where possible): mission success, task completion rate, makespan ratio, distance, replan recovery time, near-miss / minimum separation distance (new in continuous space), message-drop sensitivity.

## 4. Acceptance criteria (from the guide's extension exit gate)
The ROS 2 version must demonstrate it **preserves the same high-level safety rules**: the LLM emits task specifications only; invalid plans are rejected; task dependencies are checked; every low-level action is mediated by a deterministic robot interface. Physical deployment remains **out of scope** until hardware-specific collision avoidance, emergency stop, watchdog, battery failsafes and an operational safety review exist.

## 5. Known risks (not yet investigated)
- **Abstraction gap:** discrete one-cell moves vs continuous motion; collision/edge-swap checks do not transfer, they must be redefined with geometry and braking distance.
- **Authority conflicts:** a navigation stack's own recovery behaviours may fight the central monitor's holds/yields.
- **Timing:** asynchronous messages, QoS, and clock sources change ordering assumptions used by the deterministic tick loop.
- **Localisation uncertainty** and sensor noise are absent from the grid model.
- **Reproducibility:** Gazebo runs are less deterministic than the seeded grid runs; statistics will need more care.
- **Effort:** the guide treats this as a separate milestone of comparable size to the integration phases; it is not a small addition.

## 6. What this extension would and would not establish
*Could establish (if the tests pass):* the mission-layer logic and safety gate behave consistently when driven through ROS 2 interfaces and a continuous simulator; quantified differences from the grid model.
*Would not establish:* safety on physical robots, behaviour of a real LLM planner, or optimality of the allocation.
