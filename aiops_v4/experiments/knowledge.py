"""Public fault mechanisms and generic verification hypotheses, never event labels."""
from aiops_challenge_2026.config import load_public_config

SOURCE='https://challenge.aiops.cn/home/competition/2087843807868489822'
# Mechanisms paraphrase the public enumeration on the rules page, checked 2026-10-09.
MECHANISMS={
 ('link','delay'):'传输时延增大', ('link','rate_limit'):'链路可用速率受限', ('link','loss'):'链路丢包',
 ('firewall','acl_drop'):'ACL 决策丢弃流量', ('firewall','rate_limit'):'防火墙流量限速',
 ('firewall','port_block'):'目标端口被阻断', ('firewall','cpu_pressure'):'防火墙资源影响转发',
 ('firewall','default_route_error'):'防火墙默认下一跳错误', ('firewall','rule_order_error'):'规则匹配顺序错误',
 ('resource','cpu_pressure'):'CPU 处理能力受压力影响', ('resource','memory_pressure'):'可用内存不足',
 ('resource','disk_io_pressure'):'磁盘读写压力', ('resource','disk_space_low'):'可用存储空间不足',
 ('resource','process_pressure'):'进程资源压力', ('resource','softirq_pressure'):'网络软中断压力',
 ('routing','blackhole'):'流量无法通过路由正常转发', ('routing','bgp_session_down'):'BGP 会话中断',
 ('routing','bgp_route_flap'):'BGP 路由反复变化', ('routing','wrong_static_route'):'静态下一跳配置错误',
 ('routing','ospf6_neighbor_down'):'OSPFv3 邻居失效', ('routing','ospf6_cost_anomaly'):'OSPFv3 路径代价异常',
 ('routing','wrong_default_route'):'默认路由错误', ('routing','bgp_route_filter'):'BGP 前缀被过滤',
 ('routing','ospf6_interface_flap'):'OSPFv3 接口反复变化', ('routing','route_loop'):'路由路径循环',
 ('routing','long_path_interruption'):'多跳远端路径中断', ('service','dns_down'):'DNS 解析服务不可用',
 ('service','dns_wrong_record'):'DNS 记录指向错误', ('service','web_5xx'):'Web 服务端错误',
 ('service','web_slow'):'Web 响应变慢', ('service','auth_timeout'):'认证请求超时',
 ('service','auth_error'):'认证返回异常错误'}
HYPOTHESES={
 'link':['比较接口、路由邻接与可用业务观测的时间先后','资源压力或服务错误可能产生相似表征；缺流量细节不能排除'],
 'firewall':['对照防火墙本机资源和上下游观测','ACL/端口/规则顺序需要配置或明确流量证据，仅连通性下降不能区分'],
 'resource':['核验计量单位、参考尺度、资源变化与业务影响的时间关系','高资源使用率可能是其他故障的后果'],
 'routing':['对照实际邻居/接口/路由指标或 FRR 原文','会话异常不能自动区分配置过滤、黑洞、环路和远端路径问题'],
 'service':['使用可用请求结果、响应时间、服务原文和主机指标','网络不可达与服务不可用需区别；缺少业务观测应标明不可判定']}


def knowledge_cards():
    return dict(version='v4-public-knowledge-1',source=SOURCE,source_section='规则：类别说明、技术建议、FAQ Q3-Q7',
                checked_date='2026-10-09',purpose='说明公开类别机理，并提示核验支持、反证和缺失；不映射任何测试样本答案',
                limits=['机理不等于当前事件结论','核验建议是通用工程假设，不是官方因果判据','第二批缺 FRR/详细流指标不能当健康',
                        '聚类是运行状态；参考区间不保证健康；无实际可追溯证据不得确认'],
                cards=[dict(major_category=c['major_category'],sub_category=c['sub_category'],
                            mechanism=MECHANISMS[(c['major_category'],c['sub_category'])],
                            verification_hypotheses=HYPOTHESES[c['major_category']],
                            hypothesis_status='generic_engineering_not_official_test_evidence')
                       for c in load_public_config('fault_taxonomy')['fault_categories']])
