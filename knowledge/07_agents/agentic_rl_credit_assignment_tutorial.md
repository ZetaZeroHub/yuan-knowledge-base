## §0 TL;DR Cheat Sheet

> **Agentic RL 信用分配一句话**：终点 reward 只知道整条 trajectory 成没成；credit assignment 要回答的是“哪一步真的让任务更接近成功，哪一步只是碰巧躺赢或背锅”。

1. **最常见错分**：GRPO / PPO 把 trajectory-level advantage 广播到所有 agent token。成功轨迹里的坏工具会被奖励，失败轨迹里的好搜索会被惩罚。observation token 仍必须 mask 掉，但这只解决“更新谁”，没解决“谁该负责”。

2. **三类方法谱系**：State-anchored stepwise 在相同或相近 state 下比较 action；Process / Progress reward 额外学 step-level scorer；Intrinsic signal 从 policy 自身的不确定性或 belief 变化里找关键 turn。

3. **HGPO 的核心补丁**：同一个网页 state 不一定有同一个历史上下文。HGPO 先按 state 对齐，再按历史 context 分层，避免把“看起来一样但前置信息不同”的决策混在一起算 advantage。

4. **SPA-RL 的核心补丁**：把最终 reward 分摊成每一步 progress contribution，让 $\sum_t \hat c_t \approx R(\tau)$，再和 action 可执行性 / grounding 信号融合成 dense reward。

5. **AgentPRM 的核心补丁**：把 PRM 当成 agent 版 $Q(s,a)$，学习“在这个 state 做这个 action 后，未来回报大概多少”。它既能训练 policy，也能在 inference 时做 Best-of-N action selection。

6. **ARPO 的核心补丁**：工具返回后，模型接下来几十个 token 的 entropy 往往飙升；这些位置是高价值分叉点。ARPO 把 rollout budget 集中用在高不确定位置，并把分叉后的回报差异回传给关键 decision tokens。

7. **IGPO 的核心补丁**：每轮工具调用或搜索应让模型更接近正确答案。IGPO 用 teacher forcing 计算 ground-truth answer log-prob 的增量，把 belief improvement 变成 turn-level reward。

8. **工程判断**：能写 verifier 时先用 outcome reward + GRPO 建 baseline；credit 错分明显时，优先加轻量 intrinsic / progress signal；只有在任务复杂且数据充足时才训练独立 PRM。

## §1 问题：终点 Reward 为什么会错分 Credit

Agentic RL 的训练样本不是一段 response，而是一条 action / observation 交错的 trajectory：

```text
task
  -> action_1(search)
  -> observation_1(web snippets)
  -> action_2(open page)
  -> observation_2(page content)
  -> action_3(answer)
  -> reward R
```

标准 GRPO / PPO 会先把整条轨迹的 reward 变成 advantage，再乘到这条轨迹的所有 agent-generated tokens 上：

$$
\nabla_\theta J
\approx
\sum_{t=1}^{T} m_t A(\tau)\nabla_\theta \log \pi_\theta(a_t \mid h_t)
$$

这里 $m_t$ 是 action mask：agent 生成的 thought / action / answer token 为 1，工具 observation / prompt / padding 为 0。mask 解决的是“哪些 token 能更新 policy”，但没有解决“这些 token 各自该拿多少 credit”。

典型错分如下：

| 轨迹 | 中间动作 | 终点 reward | 广播后问题 |
|---|---|---:|---|
| $\tau_1$ | 好搜索 -> 多余工具 -> 答对 | 1 | 多余工具也吃正 credit |
| $\tau_2$ | 好搜索 -> 证据误读 -> 答错 | 0 | 好搜索也吃负 credit |
| $\tau_3$ | 错搜索 -> 无效工具 -> 答错 | 0 | 错搜索和无效工具无法区分责任 |

所以 Agentic RL 的 credit assignment 不是“要不要 mask observation”，而是“terminal signal 如何落到 step-level decision 上”。

## §2 三类方法总览

| 方法族 | 代表方法 | 信号来源 | 最适合场景 | 主要代价 |
|---|---|---|---|---|
| State-anchored stepwise | GiGPO / HGPO | 同 state 或同历史上下文下的相对回报 | web / GUI / game 里 state 可对齐 | 需要足够多 rollout 碰到相似 state |
| Process / Progress reward | SPA-RL / AgentPRM / PiCA | 额外训练的 step scorer、PRM、progress estimator | 长 horizon、终点 reward 很稀疏 | 训练 scorer；可能 reward hacking |
| Intrinsic signal | ARPO / IGPO | entropy、answer belief improvement、policy 自身变化 | search/tool-use，想少加外部模型 | 依赖不确定性或 ground truth 质量 |

三类方法不是互斥的。一个现实 pipeline 常见组合是：

```text
Outcome verifier
  + format / length shaping
  + intrinsic branch sampling
  + optional PRM / progress reward
```

其中 outcome verifier 负责把方向钉住，dense / intrinsic signal 负责让中间步骤更可学。

## §3 State-Anchored Stepwise：同局面下比较动作

### 3.1 GiGPO / State Grouping 的直觉

如果多个 rollout 来到同一个 state，就可以比较“在这个局面下不同 action 的后续回报”。设同一 state $s$ 下有 $K$ 条 continuation：

$$
\{\tau_1, \ldots, \tau_K\}, \quad \tau_i = (s, a_i, o_{i,t+1}, \ldots, R_i)
$$

可以用组内相对回报给 action 局部 advantage：

$$
\hat A(s, a_i)=
\frac{R_i-\operatorname{mean}(R_1,\ldots,R_K)}
{\operatorname{std}(R_1,\ldots,R_K)+\epsilon}
$$

这比整条轨迹广播更合理：它问的是“从同一个局面出发，哪个 action 更容易成功”。

### 3.2 HGPO：State 相同还不够

Web / GUI agent 里，“当前页面相同”不等于“决策上下文相同”。两个 agent 可能都来到购物车页面：

- 一个前面已经确认了颜色、尺码、预算、收货地址。
- 另一个只是乱点进购物车，关键约束都没读到。

如果只按当前 DOM / screenshot 分组，同一个 action 的意义会被混淆。HGPO 引入历史上下文一致性：先按 current state 分组，再按最近 $k$ 步 history 的相似度分层。

一个简化形式：

$$
G_{\ell}(s_t)
=
\{\tau_i \mid \operatorname{sim}(C_\ell(h_{i,t}), C_\ell(h_t)) \ge \delta_\ell\}
$$

其中 $C_\ell$ 是不同粒度的 context operator，例如最近 1 步、3 步、完整 task constraints。每层都能得到一个 relative advantage：

$$
\hat A_\ell(s_t,a_t)
=
\frac{R_t-\mu(G_\ell)}
{\sigma(G_\ell)+\epsilon}
$$

最后融合：

$$
\hat A(s_t,a_t)=\sum_\ell w_\ell \hat A_\ell(s_t,a_t)
$$

### 3.3 什么时候用这条线

适合：

- 环境 state 可观测、可哈希或可相似度检索。
- 同一任务会产生多条 rollout，且经常在中间 state 汇合。
- 错分主要来自“同一局面下 action 好坏不清楚”。

不适合：

- math / code 这类 state 不容易自然碰撞的任务。
- 每条轨迹状态几乎唯一，group 太小导致 $\sigma=0$ 或噪声很大。

## §4 Process / Progress Reward：给中间步骤打分

### 4.1 SPA-RL：把终点 Reward 分摊成 Progress

SPA-RL 的目标不是直接判断一步“好/坏”，而是估计这一步对最终成功贡献了多少 progress。设 progress estimator 输出 $\hat c_t$：

$$
\sum_{t=1}^{T} \hat c_t \approx R(\tau)
$$

如果最终成功，贡献应集中在真正推动任务的步骤上；如果失败，贡献应暴露哪些步骤没有推进或把任务带偏。

实际 reward 常会融合 grounding signal $g_t$：action 是否可执行、是否产生有效 observation、是否违反环境约束。

$$
r_t^{\text{SPA}} = \alpha \hat c_t + \beta g_t
$$

然后把 $r_t^{\text{SPA}}$ 放进 PPO / GRPO 的 reward tensor，让 GAE 或 group advantage 接手后续优化。

### 4.2 AgentPRM：把 PRM 当成 $Q(s,a)$

AgentPRM 更像 actor-critic：PRM 不只是评“推理步骤漂亮不漂亮”，而是近似 action value：

$$
Q_\psi(s_t,a_t) \approx \mathbb{E}[R(\tau)\mid s_t,a_t]
$$

训练流程可以分三步：

1. 用当前 policy 采样多条 agent trajectories。
2. 用终点 verifier / reward 给轨迹打分，再回填到 state-action pair。
3. 训练 PRM / Q model 预测从 $(s_t,a_t)$ 出发的未来回报。

训练 policy 时，可以用：

$$
A_t = Q_\psi(s_t,a_t)-b(s_t)
$$

其中 $b(s_t)$ 可以是同 state 的均值、value baseline，或组内归一化 baseline。

推理时 AgentPRM 还能做 Best-of-N：同一 state 采样多个候选 action，用 $Q_\psi(s,a)$ 选最高的一个执行。这会提高成功率，但也增加 inference cost。

### 4.3 PiCA：把 Pivot Step 变成 Reward Tensor

PiCA 的问题意识很直接：Search-R1 这类多跳检索任务，真正关键的是某次 pivot search 或 pivot reasoning turn。它用 step-PRM 找到“让模型从不会到会”的关键点，再把中间 reward 注入原有 PPO / GAE 流程。

这条线的工程价值是：不用大改 trainer，不一定新增 value head；只要把 terminal-only reward 改成含 step reward 的 tensor，就能让 GAE 把 credit 沿时间回流。

## §5 Intrinsic Signal：从 Policy 自己找关键点

### 5.1 ARPO：Entropy 高的位置更值得分叉

工具返回之后，模型常常进入不确定区：证据冲突、下一步工具不明确、是否该停止不明确。ARPO 用 entropy 定位这些高价值分叉点。

令第 $t$ 个 token 的 entropy 为：

$$
H_t = -\sum_y \pi_\theta(y\mid h_t)\log \pi_\theta(y\mid h_t)
$$

ARPO 会在 entropy 峰值附近追加 partial rollout：

```text
shared prefix
  -> branch_1 continuation -> R_1
  -> branch_2 continuation -> R_2
  -> branch_3 continuation -> R_3
```

共享前缀不应被所有 branch 的终点 reward 粗暴平均；关键是把分叉后的回报差异回传给触发分叉的 decision region。这样 rollout budget 用在“模型自己也拿不准”的位置，而不是平均铺到所有 token。

适合：

- search / tool-use，工具 observation 后存在明显不确定峰。
- rollout 成本高，不能每个 step 都大量采样。

风险：

- 高 entropy 不一定是关键决策，也可能只是罕见词、日期、人名。
- 所以实际实现通常还会结合未来回报差异或 task-specific filter。

### 5.2 IGPO：正确答案概率的增量

IGPO 把每一轮交互看成 belief update。若已有 ground-truth answer $y^\*$，就可以在第 $t$ 轮历史 $h_{\le t}$ 下，用 teacher forcing 计算模型生成正确答案的平均 log probability：

$$
\ell_t =
\frac{1}{|y^\*|}
\sum_{j=1}^{|y^\*|}
\log \pi_\theta(y^\*_j \mid h_{\le t}, y^\*_{<j})
$$

turn-level information gain：

$$
r_t^{\text{IG}} = \ell_t - \ell_{t-1}
$$

如果某次搜索、读页、工具调用让模型更确信正确答案，$r_t^{\text{IG}}>0$；如果带偏，$r_t^{\text{IG}}<0$。IGPO 通常仍保留 terminal outcome reward，并分别做 group-wise normalization：

$$
\hat A_t = \operatorname{norm}(R_{\text{outcome}}) + \lambda \operatorname{norm}(r_t^{\text{IG}})
$$

优点：

- 不需要额外训练 PRM。
- 信号稠密，能定位哪一轮带来信息增益。

限制：

- 依赖高质量 ground truth。
- 多答案开放任务会被单一标注误导。
- 如果模型本身 calibration 很差，log-prob 增量会有噪声。

## §6 训练接口：Credit Signal 怎么接进 PPO / GRPO

### 6.1 数据结构

一个可扩展的 agent rollout batch 至少要区分四类字段：

```python
batch = {
    "input_ids": ...,       # full trajectory tokens
    "action_mask": ...,     # 1 = policy-generated token
    "step_ids": ...,        # token -> agent step index
    "trajectory_reward": ..., # terminal / outcome reward
    "step_reward": ...,     # optional dense reward, shape [B, T_step]
    "old_log_probs": ...,
}
```

`action_mask` 仍是底线；`step_ids` 让 step-level reward 能 broadcast 到对应 action tokens；`step_reward` 可以来自 SPA-RL、AgentPRM、IGPO 或 PiCA。

### 6.2 Reward Tensor 注入

如果 step reward 已经可用，先构造 step return：

$$
r_t = r_t^{\text{dense}} + \mathbb{1}[t=T]R_{\text{outcome}}
$$

然后：

- PPO：用 `trajectory_gae(rewards, values, dones)` 得到 step advantage，再 broadcast 到该 step 的 action tokens。
- GRPO：对同 prompt 多 rollout 的 terminal reward 做 group-relative advantage，同时把 dense reward 作为 per-step correction。
- Critic-free 变体：对每条 trajectory 保留 trace-level advantage，再用 step credit 作为 multiplicative 或 additive weight。

### 6.3 最小实现骨架

```python
import torch

def broadcast_step_credit_to_tokens(step_advantage, step_ids, action_mask):
    """
    step_advantage: [B, T_step]
    step_ids:       [B, L], token 所属 step，prompt/obs/pad 可设为 -1
    action_mask:    [B, L], 只允许 agent token 更新
    """
    B, L = step_ids.shape
    token_adv = step_advantage.new_zeros(B, L)
    valid = (step_ids >= 0) & action_mask.bool()
    token_adv[valid] = step_advantage[
        torch.arange(B, device=step_ids.device).unsqueeze(1).expand(B, L)[valid],
        step_ids[valid],
    ]
    return token_adv * action_mask
```

关键 invariant：

- observation token 不参与 log-prob loss。
- dense reward 可以来自 observation 之后的判断，但只能更新导致它的 agent action。
- 如果一个 step 里包含 thought + action JSON，通常两者共享同一个 step advantage；如果要更细，可以再拆 token-level credit。

## §7 选型决策树

| 现象 | 优先尝试 | 原因 |
|---|---|---|
| group 经常全 0 / 全 1 | dynamic sampling / curriculum / easier tasks | 没有 reward 方差，任何 credit 方法都学不动 |
| 成功轨迹里有明显无效工具也被强化 | length/tool penalty + step-level credit | 先用 cheap shaping 控制坏习惯 |
| 失败轨迹里有好搜索但最终误读 | IGPO / AgentPRM / PiCA | 需要区分“信息获取对了”和“整合错了” |
| web state 经常重复出现 | HGPO | 同 state 多 rollout 可做局部相对比较 |
| 工具返回后分叉很多 | ARPO | rollout budget 应集中到高不确定区 |
| 长任务需要高质量中间监督 | SPA-RL / AgentPRM | terminal-only 太稀疏，需要学 scorer |

## §8 常见 Failure Modes

| Failure mode | 症状 | 缓解 |
|---|---|---|
| State aliasing | 当前 state 一样但历史约束不同，advantage 互相污染 | HGPO-style history grouping |
| PRM hacking | agent 学会取悦 step scorer，但终点成功率下降 | terminal verifier 保持主权重；PRM 只做辅助 |
| Entropy misfire | ARPO 分叉在罕见词、日期、人名上 | entropy + future value delta 双信号过滤 |
| Ground-truth overfit | IGPO 惩罚合理但非标注答案的路径 | 多答案 normalization；开放任务慎用 IGPO |
| Reward double counting | dense reward 和 terminal reward 重复奖励同一事件 | 分别 normalize；限制 dense reward 权重 |
| Step boundary 错 | 一个 action 的 credit 被广播到前后无关 token | 显式保存 step_ids / action spans |

## §9 高频面试题

### Q1. 为什么 GRPO 在 agent 上仍会有 credit dilution？

GRPO 省掉 critic，用组内相对 reward 做 trace-level advantage；这很稳，但一条成功轨迹的所有 agent token 共享同一个 advantage。长轨迹里真正关键的可能只有 2-3 个 action，其他 token 也被同等强化，所以会有 credit dilution。

### Q2. Process reward 和 progress reward 有什么区别？

Process reward 通常评“这一步是否正确 / 合理”；progress reward 更强调“这一步让任务向最终目标推进了多少”。SPA-RL 属于 progress redistribution，AgentPRM 更像 $Q(s,a)$ / future return estimator。

### Q3. 为什么同 state 比较 action 还要看 history？

因为 agent state 往往是部分可观测的。网页页面相同，但 agent 是否已经读过约束、是否知道用户预算、是否完成前置步骤，会改变同一个 action 的好坏含义。HGPO 解决的是 historical context inconsistency。

### Q4. IGPO 为什么不适合所有任务？

它依赖 ground-truth answer，并用正确答案 log-prob 的增量作为 reward。开放式任务、多答案任务、长代码 patch 任务很难定义一个稳定的 token-level ground truth；这时 IGPO 容易奖励“更像标注”而不是“真实更好”。

### Q5. 加了 PRM 后还需要 terminal verifier 吗？

需要。PRM 是辅助信号，容易被 hack 或学到表面相关特征；terminal verifier 是最终任务成功的锚。生产里通常让 terminal outcome 权重大于 process / progress reward。

## §A 参考来源

- 本地参考：`Agentic-RL-Most-Detailed-Intro/Agentic RL入门2：信用分配.html`
- 本地参考：`Agentic-RL-Most-Detailed-Intro/algorithms.html`
- 交叉阅读：`07_agents/agentic_rl_tutorial.md` 的 §4 Long-horizon credit assignment 与 §7 code patterns。
