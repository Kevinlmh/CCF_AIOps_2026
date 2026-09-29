# 基于多源观测数据的骨干网全栈故障诊断

**官网**：https://challenge.aiops.cn/home/competition/2087843807868489822

**baseline**：https://gitee.com/murina/aiops-challenge-2026

**提交脚本**：https://gitee.com/lin-xianghong/aiops-challenge2026-submission

**数据**：https://pan.cstcloud.cn/web/share.html?hash=r9YmfWbeTG4

**TL; DR**：**多源网络数据 → 异常发现 → 根因定位 → 故障分类”** 问题。目标是在复杂骨干网络中，根据多个设备、多个城市、多个观测源的数据，自动判断**到底哪里出了问题，以及出了什么问题**。

## 比赛目标

现实中的运营商骨干网包含很多网络设备、服务器和链路。假设某一时刻网络出现故障，你看到的通常不是：

> “北京的 br-1 发生了 XXX 故障。”

而是大量复杂的异常现象：

```
设备 A：某个指标突然升高
设备 B：网络流量下降
设备 C：出现异常日志
设备 D：若干指标同时波动
设备 E：受到上游故障影响，也出现异常
...
```

真正需要解决的是：

从大量异常现象中找到真正的故障根因
$$
\boxed{ \text{从大量异常现象中找到真正的故障根因} }
$$


并进一步判断：

这个根因属于哪一种故障类型
$$
\boxed{ \text{这个根因属于哪一种故障类型} }
$$


所以最终可以概括成**两个核心预测任务**：

根因定位+故障分类
$$
\boxed{\text{Root Cause Localization+Fault Classification}}
$$


即：

**任务 1：故障发生在哪里？**

**任务 2：发生了什么故障？**

## 具体任务

### Case

基本单位可以理解成一个：$\textbf{Fault Case}$

一个 Case 包含一段时间内整个骨干网络的观测信息。

```
Case 001
│
├── City A
│   ├── router
│   ├── firewall
│   ├── server
│   └── 多种观测数据
│
├── City B
│   ├── router
│   ├── firewall
│   ├── server
│   └── 多种观测数据
│
├── ...
│
└── City H
```

### 输入

多源观测数据 + 复杂网络拓扑
$$
X={X^{(1)},X^{(2)},...,X^{(M)}}
$$
 $X^{(m)}$ 表示不同来源的网络观测信息

### 任务1：根因定位(排序问题)

假设网络里有：

```
A —— B —— C —— D
          |
          E
```

真正坏掉的是：

```
C
```

但 C 的故障可能导致：

```
B abnormal
C abnormal
D abnormal
E abnormal
```

所以：

> **异常设备 ≠ 根因设备。**

算法需要在所有候选网络元素中找到最可能的真正根因。

网络元素集合：
$$
V=\{v_1,v_2,\ldots,v_N\}
$$


根据一个 case 的所有观测数据 $X$，计算：
$$
P(v_i=\text{root cause}\mid X)
$$


然后排序：
$$
v_{(1)},v_{(2)},...,v_{(N)}
$$


即 **Top-K 根因候选排序**。

### 任务2：故障分类（分类问题）


$$
P(c_j\mid X,v_{root})
$$
其中，$c_j\in \{\text{Fault Type}_1,\text{Fault Type}_2,…\}$

整体任务流程

```
Root Cause
   ↓
cityA-br-1
   ↓
到底是什么问题？
   ↓
Fault Type
```

### 整体形式化定义

**输入**：
$$
X_i= \{ \text{所有网络元素在一段时间内的多源观测} \}
$$


$$\{City,N,T\}$$

需要输出：

**输出 1：Root Cause 排名**
$$
R_i = [r_1,r_2,\ldots,r_5]
$$


即 Top-5 最可能的根因网络元素。

**输出 2：Fault Category**
$$
y_i=c
$$
即该 Case 对应的故障类型。

因此整个模型：
$$
f(X_i)→(R_i,y_i)
$$

## 具体分工

### 模型 @lmh

1. Baseline在已有的数据集先跑通；

2. 搞清楚输入和输出的格式

   1. 交付：输入格式文件类型，
   2. 输入维度（B,T,F）类似，输出维度
   3. 输出文件类型

3. 搞清楚baseline的流程和如何修改

   ```
   run.py
      ↓
   谁调用 preprocessing？

   preprocessing
      ↓
   输出什么结构？

   five_sigma.py
      ↓
   detect() 输出什么？

   localization
      ↓
   输入异常点后怎么排名？

   classification
      ↓
   怎么构造 LLM prompt？
   ```



### 数据 @dyx

1. 数据基本情况统计，形成数据的pipeline
2. 字段统计：字段的含义
3. 具体搞清楚多源数据都有哪些源头，对应的含义是什么
4. 归因case，给出为什么定位到这个错误，为什么分类成这个错误
