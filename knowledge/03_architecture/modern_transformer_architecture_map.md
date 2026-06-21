## §0 TL;DR Cheat Sheet

> **Modern Transformer Architecture Map** 是一张“当代基座模型组件地图”：位置编码、attention 变体、归一化、MoE、MTP、KV cache 各自解决不同瓶颈，不能混成一个技术点。

1. **位置编码层**：RoPE 是事实标准；长上下文常用大 base + YaRN / Dynamic NTK；MLA 系必须做 decoupled RoPE，因为 RoPE 旋转矩阵不能被低秩 K/V 投影安全吸收。

2. **KV cache 层**：MHA -> MQA -> GQA 是减少 KV head；MLA 是把 K/V 压成 latent；两者都主要服务 decode 阶段的显存和带宽，不等价于“减少所有 attention FLOPs”。

3. **稀疏 attention 层**：Sliding Window、StreamingLLM、NSA、DSA、MoBA 都在削 $O(L^2)$ 或长上下文访存；它们和 RoPE 外推、KV cache 压缩是正交维度。

4. **Linear / SSM hybrid 层**：Mamba / Mamba-2 / linear attention 把长序列复杂度降到近线性，但纯 SSM 难覆盖所有 retrieval / in-context learning 行为，所以现实路线多是 hybrid。

5. **归一化层**：RMSNorm 是 decoder-only LLM 主流；QK-Norm / soft-capping 主要防 attention logits 失控和 loss spike；DeepNorm / LayerScale / μP 属于深层稳定训练工具箱。

6. **MoE 层**：DeepSeekMoE / Qwen-MoE 的重点不是“参数多”，而是 fine-grained expert、shared expert、aux-loss-free balance、EP all-to-all 等训练和推理工程。

7. **MTP 层**：Multi-Token Prediction 给 next-token objective 加密集监督，也可在推理时作为 draft head 做 speculative decoding；它和 Medusa / EAGLE 的关系是“训练目标 + 推理加速头”的桥。

8. **推理系统层**：Prefill 是 compute-bound，decode 是 memory-bandwidth-bound。现代 serving 优化通常把 chunked prefill、paged KV、speculative decoding、KV quant、GQA/MLA 组合使用。

## §1 总览：六个正交问题

| 问题 | 代表组件 | 主要收益 | 常见误解 |
|---|---|---|---|
| 位置如何表示 | RoPE / YaRN / Dynamic NTK | 长上下文外推 | 以为它会降低 KV cache |
| KV 怎么存 | MQA / GQA / MLA | decode 显存和带宽 | 以为它等价减少 Q/O 计算 |
| 注意力怎么稀疏 | SWA / NSA / DSA / MoBA | 降 $L^2$ 成本 | 以为稀疏注意力自动有全局记忆 |
| 长序列怎么替代 attention | Linear Attention / SSM / Hybrid | 近线性复杂度 | 以为纯 SSM 一定替代 Transformer |
| 训练怎么稳定 | RMSNorm / QK-Norm / soft-capping / DeepNorm | 防 logit spike、梯度不稳 | 以为 normalization 只影响收敛速度 |
| 容量怎么扩 | MoE / MTP | 参数容量、dense supervision、推理加速 | 以为 sparse activation 免费 |

现代基座通常不是“选择一个技术”，而是在每层叠配方。例如 DeepSeek-V 系可以同时用 MLA、decoupled RoPE、MoE、MTP、aux-loss-free routing；Qwen / Llama / Mistral 系则常见 GQA + 大 RoPE base + YaRN + SWA 或推理系统优化。

## §2 位置编码：RoPE 到 Decoupled RoPE

### 2.1 RoPE 的核心不变量

RoPE 对 Q/K 做二维旋转，使 attention score 依赖相对位置：

$$
(R_m q)^\top(R_n k)=q^\top R_{n-m}k
$$

因此位置信息进入 $q^\top k$ 打分，而不是进入 V。V 是被加权求和的内容，旋转 V 一般没有收益。

### 2.2 长上下文扩展配方

| 方法 | 做法 | 优点 | 代价 |
|---|---|---|---|
| PI | 把位置 $m$ 压到 $m/s$ | 实现最简单 | 高频分辨率受损 |
| NTK-aware | 改 RoPE base，让低频更适配长距 | 零样本外推较稳 | 频段控制粗 |
| YaRN | 分频段缩放 + attention temperature | 工业常用，兼顾短/长距 | 需要短微调更稳 |
| Dynamic NTK | 根据当前长度动态改 scaling | 服务端适应变长请求 | 实现要处理 cache 一致性 |

### 2.3 Decoupled RoPE 与 MLA

MLA 把 K/V 压成 latent cache：

$$
c_t^{KV}=W^{DKV}h_t
$$

如果直接对 latent K 套 RoPE，旋转矩阵会随相对位置变化，不能被吸收到固定投影矩阵里，MLA 的 inference trick 会失效。解法是把 attention head 分成两段：

$$
q^\top k
=
q_{\text{rope}}^\top k_{\text{rope}}
+
q_{\text{nope}}^\top k_{\text{nope}}
$$

其中小维度 RoPE key 单独缓存并跨 head 共享，大部分内容通道走低秩 latent，不施加 RoPE。

## §3 Attention 全家桶

### 3.1 MHA / MQA / GQA / MLA

| 变体 | K/V heads 或 cache | 主要收益 | 用法 |
|---|---|---|---|
| MHA | 每个 Q head 独立 K/V | 质量基线 | 小模型或训练基线 |
| MQA | 所有 Q head 共享 1 组 K/V | KV cache 最省 | 质量可能受损 |
| GQA | $G$ 组 K/V，被多组 Q 共享 | 质量/显存折中 | Llama / Mistral / Qwen 常见 |
| MLA | cache 低秩 latent + RoPE 通道 | 比 GQA 更激进压缩 KV | DeepSeek-V 系 |

要点：MQA/GQA 改的是 K/V head 数；MLA 改的是 K/V 表示本身。说“MLA 是极端 GQA”不准确。

### 3.2 Sliding Window 与流式注意力

Sliding Window Attention 只看最近 $W$ 个 token，把每层 attention 从 $O(L^2)$ 降成 $O(LW)$。多层堆叠后感受野可以扩散，但单层仍看不到 window 外。

StreamingLLM 保留少量 attention sink token + 最近窗口，让模型可以长时间流式生成而不爆 KV cache。但它不是完整长上下文：window 外内容被丢掉后，模型真的看不到。

### 3.3 DSA / NSA / MoBA

这些方法都在试图让稀疏注意力更“原生”：

| 方法 | 核心想法 | 适合记忆点 |
|---|---|---|
| DSA | 用可学习或结构化稀疏模式替代 dense attention | 稀疏不是后处理，而是训练内生 |
| NSA | compressed / selected / sliding 三类分支并行 | 同时保留局部、压缩全局和关键块选择 |
| MoBA | 把序列分块，按 block routing 选择 top-k block | 像 MoE 一样给 attention blocks 做路由 |

它们和 Longformer / BigBird 的传统稀疏 pattern 差别在于：现代方法更强调端到端训练、动态选择和与 KV cache / serving 的兼容。

### 3.4 Linear Attention / SSM / Hybrid

Linear attention 把 softmax attention 改写为 kernel feature 累积：

$$
\operatorname{Attn}(Q,K,V)_t
\approx
\frac{\phi(q_t)^\top \sum_{i\le t}\phi(k_i)v_i^\top}
{\phi(q_t)^\top \sum_{i\le t}\phi(k_i)}
$$

SSM / Mamba 系用选择性状态空间和并行 scan 表达长序列依赖。优势是长序列近线性；短板是强 retrieval、精确复制、复杂 in-context binding 仍常依赖 attention。因此现实基座常用 hybrid：部分层是 attention，部分层是 SSM / linear attention。

## §4 归一化与训练稳定

### 4.1 RMSNorm 是 decoder-only 主流默认

RMSNorm 去掉均值中心化，只按均方根缩放：

$$
\operatorname{RMSNorm}(x)=
\frac{x}{\sqrt{\frac{1}{d}\sum_i x_i^2+\epsilon}}\odot g
$$

它比 LayerNorm 更省算，和 pre-norm residual stack 配合稳定，已成为很多 decoder-only LLM 的默认选择。

### 4.2 QK-Norm 与 Soft-Capping

大模型训练的 attention logit spike 常来自 $q \cdot k$ 范数失控。QK-Norm 在 Q/K 上做归一化，让 attention logits 更可控：

$$
\tilde q = \frac{q}{\|q\|}, \quad
\tilde k = \frac{k}{\|k\|}
$$

Soft-capping 则对 logits 做平滑限幅，例如：

$$
\operatorname{softcap}(x)=c\tanh(x/c)
$$

直觉：不要让极端 logit 把 softmax 变成几乎 one-hot，从而导致梯度和 loss spike。

### 4.3 DeepNorm / LayerScale / μP

| 方法 | 用途 | 一句话 |
|---|---|---|
| DeepNorm | 超深 Transformer 稳定训练 | 调 residual 分支尺度，让深层梯度更稳 |
| LayerScale | 残差分支可学习小尺度 | 从接近恒等映射开始训练 |
| μP | 宽度扩展时保持超参可迁移 | 用参数化规则让小模型调参能迁移到大模型 |

这些组件不一定每个现代基座都用，但它们属于“训练越深越大时必须懂的稳定性工具箱”。

## §5 MoE：容量、路由与系统

MoE 的核心不是简单把参数变多，而是每个 token 只激活少数 experts：

$$
y=\sum_{i\in \operatorname{TopK}(g(x))}\tilde g_i(x)E_i(x)
$$

现代 MoE 面试常考四点：

1. **Fine-grained experts**：把大 expert 拆成更多小 expert，每 token 选更多小 expert，提高组合性。
2. **Shared experts**：少数 shared expert 对所有 token 激活，吸收通用能力，routed experts 更专精。
3. **Aux-loss-free balance**：通过 expert bias 调 top-k 选择，不把 load-balance loss 的干扰梯度加进主训练。
4. **Expert Parallelism**：token dispatch / combine 需要 all-to-all，通信和负载均衡决定真实吞吐。

交叉阅读：`03_architecture/moe_tutorial.md` 已经有完整推导、代码和 DeepSeek-V3 系统细节。

## §6 MTP：Multi-Token Prediction

Next-token prediction 每个位置只监督下一个 token；MTP 让同一个 hidden state 预测未来多个 token：

$$
\mathcal{L}_{\text{MTP}}
=
\sum_{k=1}^{K}\lambda_k
\operatorname{CE}(p_\theta(x_{t+k}\mid h_t), x_{t+k})
$$

收益：

- 训练时给更密集的未来 token 监督。
- 推理时 MTP head 可以作为 draft model，配合 speculative decoding。
- 对 reasoning / code 这类长依赖任务，MTP 可能帮助模型提前规划局部 token 序列。

与 Medusa / EAGLE 对比：

| 方法 | 核心 | 训练位置 |
|---|---|---|
| MTP | 主模型附带多步预测目标 | 预训练 / continued training |
| Medusa | 多个并行 draft heads | 常作为推理加速 finetune |
| EAGLE | 用 feature-level extrapolation draft | 推理加速模型 |

## §7 Prefill / Decode 与 Serving 组合

### 7.1 Prefill vs Decode

| 阶段 | 输入 | 瓶颈 | 典型优化 |
|---|---|---|---|
| Prefill | 长 prompt 一次算完 | compute-bound，$L^2$ attention | FlashAttention、chunked prefill、context parallel |
| Decode | 每步生成 1 token | memory-bandwidth-bound，读权重和 KV | KV cache、GQA/MLA、PagedAttention、KV quant |

这解释了为什么同一项技术在两个阶段效果不同：weight-only quant 对 decode 很有用，但 prefill 可能不明显；KV cache 压缩主要救 decode，而不是 prefill 的 $L^2$ attention。

### 7.2 现代 Serving Stack

一个现实组合常长这样：

```text
Architecture:
  GQA or MLA
  + RoPE scaling / YaRN
  + optional SWA / sparse attention

Runtime:
  Paged KV cache
  + chunked prefill
  + continuous batching
  + speculative decoding
  + KV quantization
```

不要把 architecture 和 runtime 混在一起。GQA/MLA 是模型结构；PagedAttention / continuous batching 是 serving 系统；speculative decoding 可以用 MTP / Medusa / EAGLE 提供 draft。

## §8 面试速查表

| 问题 | 30 秒答案 |
|---|---|
| MLA 和 GQA 区别？ | GQA 共享 K/V heads；MLA cache 低秩 latent，并用 decoupled RoPE 保留位置通道。 |
| YaRN 解决什么？ | RoPE 长上下文外推：分频段缩放 + attention temperature，兼顾高频近距和低频远距。 |
| NSA / MoBA 和 SWA 区别？ | SWA 是固定局部窗口；NSA / MoBA 更强调动态选择、压缩全局和端到端稀疏路由。 |
| QK-Norm 有什么用？ | 控制 Q/K 范数，防 attention logits spike，提升大模型训练稳定性。 |
| MTP 是训练还是推理技术？ | 两者都有：训练时多步预测辅助目标，推理时可作为 draft head 做 speculative decoding。 |
| Prefill 和 decode 瓶颈？ | Prefill compute-bound；decode memory-bandwidth-bound。 |

## §A 参考来源

- 本地参考：`Agentic-RL-Most-Detailed-Intro/Agentic RL入门3：transformer架构.html`
- 交叉阅读：`01_general/attention_tutorial.md`
- 交叉阅读：`01_general/kv_cache_speculative_decoding_tutorial.md`
- 交叉阅读：`03_architecture/long_context_rope_yarn_mla_tutorial.md`
- 交叉阅读：`03_architecture/moe_tutorial.md`
