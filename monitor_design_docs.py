"""Technical design documentation HTML for the Web「设计」page."""
from __future__ import annotations

# Each pipeline module is documented with five dimensions:
# 职责 | 计算 | 指标含义 | 输出 | 后续用途

_DIM_CSS = """
	<style>
	.doc-dim{margin:14px 0 0;padding:12px 14px;border-left:3px solid #6366f1;background:#f8fafc;border-radius:0 10px 10px 0}
	.doc-dim h4{margin:0 0 6px;font-size:13px;color:#4338ca;text-transform:none;letter-spacing:.02em}
	.doc-dim p,.doc-dim ul{margin:0;font-size:13px;line-height:1.55;color:#334155}
	.doc-dim ul{padding-left:18px}
	.doc-module{margin-bottom:8px}
	.doc-module>h3{margin-top:20px;font-size:15px}
	.indicator-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:12px;margin-top:12px}
	.indicator-card{padding:14px;border:1px solid #e2e8f0;border-radius:12px;background:#fff}
	.indicator-card h5{display:flex;align-items:center;gap:8px;margin:0 0 8px;font-size:14px;color:#0f172a}
	.indicator-card h5 span{padding:2px 7px;border-radius:999px;background:#eef2ff;color:#4338ca;font-size:11px;font-weight:700}
	.indicator-card p{margin:5px 0;font-size:13px;line-height:1.55;color:#475569}
	.indicator-card .indicator-read{color:#0f172a}
	.indicator-card .indicator-note{padding-top:7px;border-top:1px dashed #e2e8f0;color:#64748b}
	.arch-map{margin:14px 0 18px;padding:16px;border:1px solid #cbd5e1;border-radius:14px;background:linear-gradient(180deg,#f8fafc,#fff);overflow-x:auto}
	.arch-title{margin:0 0 10px;text-align:center;font-size:13px;font-weight:800;color:#334155}
	.arch-row{display:flex;align-items:stretch;justify-content:center;gap:8px;min-width:920px}
	.arch-box{flex:1;min-width:128px;padding:10px 9px;border:1px solid #cbd5e1;border-radius:10px;background:#fff;text-align:center;box-shadow:0 2px 8px rgba(15,23,42,.05)}
	.arch-box strong{display:block;margin-bottom:5px;font-size:12px;color:#0f172a}
	.arch-box small{display:block;font-size:11px;line-height:1.45;color:#64748b}
	.arch-box.control{border-color:#93c5fd;background:#eff6ff}
	.arch-box.input{border-color:#67e8f9;background:#ecfeff}
	.arch-box.core{border-color:#a5b4fc;background:#eef2ff}
	.arch-box.ai{border-color:#c4b5fd;background:#f5f3ff}
	.arch-box.output{border-color:#86efac;background:#f0fdf4}
	.arch-box.store{border-color:#fcd34d;background:#fffbeb}
	.arch-box.web{border-color:#f9a8d4;background:#fdf2f8}
	.arch-arrow{display:flex;align-items:center;justify-content:center;flex:0 0 24px;color:#64748b;font-size:19px;font-weight:800}
	.arch-down{height:28px;display:flex;align-items:center;justify-content:center;color:#64748b;font-size:21px;font-weight:800}
	.arch-split{display:grid;grid-template-columns:1fr 1fr;gap:12px;min-width:920px}
	.arch-lane{padding:12px;border:1px dashed #94a3b8;border-radius:12px;background:rgba(255,255,255,.72)}
	.arch-lane h5{margin:0 0 8px;text-align:center;font-size:12px;color:#334155}
	.arch-note{margin:10px 0 0;font-size:11px;line-height:1.55;color:#64748b;text-align:center}
	.arch-legend{display:flex;flex-wrap:wrap;gap:8px;margin:10px 0 0;justify-content:center}
	.arch-legend span{padding:3px 8px;border-radius:999px;border:1px solid #cbd5e1;background:#fff;font-size:11px;color:#475569}
	@media(max-width:900px){.arch-map{padding:12px}.arch-row,.arch-split{min-width:800px}}
	</style>
"""


def _mod(title: str, duty: str, calc: str, meaning: str, output: str, usage: str) -> str:
    return f"""
	<div class="doc-module">
	<h3>{title}</h3>
	<div class="doc-dim"><h4>职责</h4><p>{duty}</p></div>
	<div class="doc-dim"><h4>计算</h4>{calc}</div>
	<div class="doc-dim"><h4>指标含义</h4>{meaning}</div>
	<div class="doc-dim"><h4>输出</h4>{output}</div>
	<div class="doc-dim"><h4>后续用途</h4>{usage}</div>
	</div>"""


def render_design_docs_html() -> str:
    parts = [
        """
	<div class="page-panel" data-page="design">
	<section class="card toolbar-card"><div><h2>设计</h2>
	<p class="section-sub">架构、单轮链路与模块说明（职责·计算·指标·输出·用途）。操作步骤见「帮助」页。</p></div><div class="toolbar-right"><a class="button btn-view" href="#help">返回操作手册</a></div></section>
	""",
        _DIM_CSS,
        """<div class="help-panel doc-panel">

	<section class="card help-card"><h2>0. 总览</h2>
	<h3>0.1 五维说明（读每个模块时对照）</h3>
	<table class="help-table"><thead><tr><th>维度</th><th>回答的问题</th></tr></thead><tbody>
	<tr><td><strong>职责</strong></td><td>这一层在系统里<strong>负责什么、不负责什么</strong></td></tr>
	<tr><td><strong>计算</strong></td><td>输入是什么、经过哪些公式/阈值/顺序得到结果</td></tr>
	<tr><td><strong>指标含义</strong></td><td>关键字段/标签在交易语义上<strong>代表什么</strong>（便于对照日志）</td></tr>
	<tr><td><strong>输出</strong></td><td>写入 snapshot / score / final_decision / JSONL 的<strong>结构</strong></td></tr>
	<tr><td><strong>后续用途</strong></td><td>谁消费、影响推送/AI/压测/模拟的哪一环</td></tr>
	</tbody></table>
	<h3>0.2 权威数据与双轨</h3>
	<ul class="help-list">
	<li><strong>推送</strong> → 以 <code>final_decision</code> 为准（AI 成功时以 <code>forward_view</code> 为操作方向）。</li>
	<li><strong>Web 图表 / 压测方向 / 模拟换仓</strong> → AI启用时使用 <code>final_decision</code>；日志明确记录 <code>ai_enabled=false</code> 时使用原始本地 <code>score.final_direction</code> 与本地入场计划。</li>
	<li><strong>本地确认</strong> → <code>score</code>、<code>local_screening</code>、<code>structure_forecast</code>；未调 AI 或 AI 失败时保留 <code>score.final_direction</code>，仅当本地交易分达到方向推送阈值再加 5 分时，才允许本地推 trade。</li>
	<li><strong>双轨前瞻</strong>：本地 <code>structure_forecast</code>（演变）与 AI <code>forward_view</code> 并行；默认 <code>forward_require_forecast_alignment</code> 要求同向才推 trade/演变。</li>
	</ul>
	<h3>0.3 单轮链路（<code>_process_inst</code>，每 <code>interval</code> 秒）</h3>
	<div class="flow-chain"><span>采集</span><span>检测</span><span>评分</span><span>AI触发</span><span>AI</span><span>merge</span><span>复核</span><span>跟踪</span><span>推送</span><span>日志</span></div>
	<table class="help-table"><thead><tr><th>顺序</th><th>函数</th><th>见章节</th></tr></thead><tbody>
	<tr><td>1</td><td><code>collect_snapshot</code></td><td>§A</td></tr>
	<tr><td>2</td><td><code>detect_signals</code></td><td>§B</td></tr>
	<tr><td>3</td><td><code>score_snapshot</code></td><td>§C · §D</td></tr>
	<tr><td>4</td><td><code>evaluate_ai_trigger</code></td><td>§E</td></tr>
	<tr><td>5</td><td><code>analyze_with_ai</code></td><td>§F</td></tr>
	<tr><td>6</td><td><code>merge_final_decision</code></td><td>§G</td></tr>
	<tr><td>7</td><td><code>_apply_decision_post_audit</code></td><td>§H</td></tr>
	<tr><td>8</td><td><code>update_*_tracking</code> · <code>update_paper_account</code></td><td>§I</td></tr>
	<tr><td>9</td><td><code>dispatch_wechat_push_if_needed</code></td><td>§J</td></tr>
	<tr><td>10</td><td><code>log_result</code></td><td>§K</td></tr>
	</tbody></table>

	<h3>0.4 整个项目关系流程图</h3>
	<p>下面第一张图回答“项目里的文件、进程、外部服务和日志如何连接”；第二张图回答“一个币种的一轮数据具体经过哪些计算”。两张图结合阅读：第一张看系统边界，第二张看分析顺序。</p>

	<h4>图一：项目总架构与数据关系</h4>
	<div class="arch-map">
		<div class="arch-title">用户控制与进程启动</div>
		<div class="arch-row">
			<div class="arch-box control"><strong>tray_launcher.py</strong><small>托盘入口<br>启动/唤醒 Web 面板</small></div>
			<div class="arch-arrow">→</div>
			<div class="arch-box control"><strong>web_control_panel.py</strong><small>配置、启停、状态、日志、压测、回放、设计页</small></div>
			<div class="arch-arrow">→</div>
			<div class="arch-box control"><strong>配置与环境变量</strong><small>trading_assistant_config.json<br>AI Key / SendKey / 策略参数</small></div>
			<div class="arch-arrow">→</div>
			<div class="arch-box control"><strong>monitor_config_summary.py</strong><small>默认值、配置快照<br>日志口径说明</small></div>
		</div>

		<div class="arch-down">↓ 启动子进程并传入配置</div>

		<div class="arch-split">
			<div class="arch-lane">
				<h5>实时运行输入</h5>
				<div class="arch-row" style="min-width:0">
					<div class="arch-box input"><strong>OKX 公共接口</strong><small>Ticker、K线、OI、费率、多空比、盘口</small></div>
					<div class="arch-arrow">→</div>
					<div class="arch-box core"><strong>okx_signal_monitor.py</strong><small>实时轮询 run_once<br>每币种调用 _process_inst</small></div>
				</div>
			</div>
			<div class="arch-lane">
				<h5>离线回放输入</h5>
				<div class="arch-row" style="min-width:0">
					<div class="arch-box input"><strong>replay_dataset.jsonl</strong><small>实时阶段录制的原始 snapshot 输入帧</small></div>
					<div class="arch-arrow">→</div>
					<div class="arch-box core"><strong>okx_signal_monitor.py</strong><small>run_replay<br>仍复用同一个 _process_inst</small></div>
				</div>
			</div>
		</div>

		<div class="arch-down">↓ 同一分析内核</div>

		<div class="arch-row">
			<div class="arch-box core"><strong>行情画像</strong><small>trend_profiles<br>market_context</small></div>
			<div class="arch-arrow">→</div>
			<div class="arch-box core"><strong>本地分析</strong><small>signals、raw_direction<br>八层评分、final_direction</small></div>
			<div class="arch-arrow">→</div>
			<div class="arch-box core"><strong>本地前瞻</strong><small>structure_forecast<br>策略视图、入场计划</small></div>
			<div class="arch-arrow">→</div>
			<div class="arch-box ai"><strong>AI 决策链</strong><small>L0–L3触发、OpenAI调用<br>merge、post_audit</small></div>
			<div class="arch-arrow">→</div>
			<div class="arch-box output"><strong>输出与跟踪</strong><small>推送门禁、模拟账户<br>信号/预测/决策结算</small></div>
		</div>

		<div class="arch-down">↓ 写入运行状态与历史结果</div>

		<div class="arch-row">
			<div class="arch-box store"><strong>分析日志</strong><small>okx_signal_analysis.jsonl<br>replay_analysis.jsonl</small></div>
			<div class="arch-box store"><strong>控制台日志</strong><small>signal_monitor_console.log<br>replay_console.log</small></div>
			<div class="arch-box store"><strong>模拟账户</strong><small>paper_account.json</small></div>
			<div class="arch-box store"><strong>表现与校准</strong><small>signal/forecast/decision<br>performance + calibration_state</small></div>
			<div class="arch-box output"><strong>外部通知</strong><small>Server酱 → 微信<br>OpenAI异常运维告警</small></div>
		</div>

		<div class="arch-down">↓ 被 Web 读取和解释</div>

		<div class="arch-row">
			<div class="arch-box web"><strong>实时监控页</strong><small>K线、最新分析快照<br>进程与配置状态</small></div>
			<div class="arch-box web"><strong>预测压测页</strong><small>价格曲线、模拟账户<br>命中率、应推送标记</small></div>
			<div class="arch-box web"><strong>回放验证页</strong><small>同一数据重复运行<br>push_analysis 理论推送</small></div>
			<div class="arch-box web"><strong>日志与诊断</strong><small>运行日志、诊断包<br>AI/微信连接测试</small></div>
			<div class="arch-box web"><strong>设计文档</strong><small>monitor_design_docs.py<br>由 Web 动态渲染</small></div>
		</div>

		<div class="arch-legend">
			<span>蓝：控制/配置</span><span>青：外部或回放输入</span><span>靛：本地分析内核</span>
			<span>紫：AI链路</span><span>绿：输出</span><span>黄：持久化</span><span>粉：Web消费</span>
		</div>
		<div class="arch-note">核心原则：Web 面板负责控制与展示，监控子进程负责计算；实时和回放使用不同输入，但共用同一分析函数；JSONL 与状态文件把运行核心连接到压测、诊断和后续校准。</div>
	</div>

	<h4>图二：单币种单轮运行流程</h4>
	<div class="arch-map">
		<div class="arch-row">
			<div class="arch-box input"><strong>输入帧</strong><small>实时 OKX<br>或 replay frame</small></div>
			<div class="arch-arrow">→</div>
			<div class="arch-box core"><strong>collect_snapshot</strong><small>价格、K线、衍生品<br>盘口、数据质量</small></div>
			<div class="arch-arrow">→</div>
			<div class="arch-box core"><strong>技术画像</strong><small>EMA/ATR/RSI/MACD<br>KDJ/BOLL/ADX</small></div>
			<div class="arch-arrow">→</div>
			<div class="arch-box core"><strong>market_context</strong><small>regime、bias<br>价格压力、情绪</small></div>
		</div>

		<div class="arch-down">↓</div>

		<div class="arch-row">
			<div class="arch-box core"><strong>detect_signals</strong><small>突破、放量、背离<br>OI/费率/盘口异动</small></div>
			<div class="arch-arrow">→</div>
			<div class="arch-box core"><strong>raw_direction</strong><small>按策略周期产生<br>较早多/空/观望</small></div>
			<div class="arch-arrow">→</div>
			<div class="arch-box core"><strong>八层评分</strong><small>direction / execution<br>risk / raw_total</small></div>
			<div class="arch-arrow">→</div>
			<div class="arch-box core"><strong>本地确认</strong><small>guard + 入场质量<br>score.final_direction</small></div>
			<div class="arch-arrow">→</div>
			<div class="arch-box core"><strong>结构演变</strong><small>structure_forecast<br>独立本地前瞻轨</small></div>
		</div>

		<div class="arch-down">↓ evaluate_ai_trigger：signals + 方向 + 分数 + forecast</div>

		<div class="arch-split">
			<div class="arch-lane">
				<h5>不调用 AI：L0/L1、资格不足或指纹冷却</h5>
				<div class="arch-row" style="min-width:0">
					<div class="arch-box ai"><strong>local_screening</strong><small>保留本地确认方向<br>高门槛决定本地推送资格</small></div>
					<div class="arch-arrow">→</div>
					<div class="arch-box output"><strong>final_decision</strong><small>direction=本地 final<br>高门槛才产生 local trade</small></div>
				</div>
			</div>
			<div class="arch-lane">
				<h5>调用 AI：L2/L3 + 资格通过 + 去重允许</h5>
				<div class="arch-row" style="min-width:0">
					<div class="arch-box ai"><strong>OpenAI 前瞻</strong><small>独立读取市场事实<br>返回 forward_view</small></div>
					<div class="arch-arrow">→</div>
					<div class="arch-box ai"><strong>校验与 merge</strong><small>有效JSON采用AI<br>失败则 local_fallback</small></div>
				</div>
			</div>
		</div>

		<div class="arch-down">↓ 汇合</div>

		<div class="arch-row">
			<div class="arch-box ai"><strong>post_audit</strong><small>压力/scalp/guard<br>forecast对齐/历史校准</small></div>
			<div class="arch-arrow">→</div>
			<div class="arch-box output"><strong>final_decision</strong><small>最终方向、置信度<br>recommendation</small></div>
			<div class="arch-arrow">→</div>
			<div class="arch-box output"><strong>跟踪与模拟</strong><small>signal/forecast/decision<br>paper_account</small></div>
			<div class="arch-arrow">→</div>
			<div class="arch-box output"><strong>双推送轨</strong><small>confirmed<br>forecast</small></div>
			<div class="arch-arrow">→</div>
			<div class="arch-box output"><strong>微信门禁</strong><small>阈值、优先级<br>冷却、SendKey</small></div>
		</div>

		<div class="arch-down">↓</div>

		<div class="arch-row">
			<div class="arch-box store"><strong>log_result</strong><small>完整帧写入 JSONL<br>作为历史权威记录</small></div>
			<div class="arch-arrow">→</div>
			<div class="arch-box web"><strong>压测与诊断</strong><small>验证窗口成熟后统计<br>命中率、推送标记、诊断包</small></div>
		</div>
		<div class="arch-note">压测与图表只读取已写入 JSONL 的历史结果，不参与当前轮评分或 AI 调用。</div>
	</div>

	<h4>图三：主要文件职责与依赖</h4>
	<table class="help-table"><thead><tr><th>文件/目录</th><th>角色</th><th>主要依赖/被谁调用</th></tr></thead><tbody>
	<tr><td><code>tray_launcher.py</code></td><td>Windows 托盘与面板生命周期入口</td><td>启动 <code>web_control_panel.py</code></td></tr>
	<tr><td><code>web_control_panel.py</code></td><td>HTTP Web控制台、配置保存、子进程管理、日志/压测/回放 API</td><td>启动 <code>okx_signal_monitor.py</code>；读取 runtime_logs；渲染设计文档</td></tr>
	<tr><td><code>okx_signal_monitor.py</code></td><td>行情采集、本地分析、AI、推送、跟踪、模拟、日志的核心</td><td>访问 OKX/OpenAI/Server酱；读配置；写 runtime_logs</td></tr>
	<tr><td><code>monitor_config_summary.py</code></td><td>默认行为、配置快照和字段说明</td><td>被监控进程与文档口径引用</td></tr>
	<tr><td><code>monitor_design_docs.py</code></td><td>设计页 HTML 内容</td><td>被 Web 面板动态渲染，不参与交易计算</td></tr>
	<tr><td><code>runtime_identity.py</code></td><td>运行目录与安装身份识别</td><td>帮助 Web/启动器选择正确配置和状态目录</td></tr>
	<tr><td><code>build/runtime_logs/</code></td><td>运行数据总线与历史状态</td><td>监控写入；Web、压测、回放和诊断读取</td></tr>
	<tr><td><code>quality_tests/</code></td><td>指标、配置、决策和日志轮转回归测试</td><td>验证核心行为，不参与生产运行</td></tr>
	</tbody></table>


	<h3>0.5 从本地分析到微信推送</h3>
	<p>每一轮监控先在本地完成行情采集、信号检测与八层评分，再按 L0–L3 决定是否调用 AI，合并为 <code>final_decision</code>，经 post-audit 与双轨推送门禁后，才可能发送微信。<strong>日志里的方向结论、压测图上的价格/模拟曲线、实际微信推送是三个不同层级</strong>，不能画等号。</p>

	<table class="help-table"><thead><tr><th>阶段</th><th>主要字段</th><th>含义</th><th>是否直接决定微信</th></tr></thead><tbody>
	<tr><td>本地预测</td><td><code>score.raw_direction</code></td><td>策略规则给出的较早多/空/观望倾向</td><td>否</td></tr>
	<tr><td>本地确认</td><td><code>score.final_direction</code>、<code>final_trade_score</code></td><td>经 guard、入场质量与方向分门槛后的本地执行方向</td><td>否；AI 关闭且分数达门槛时才可能本地 trade</td></tr>
	<tr><td>结构演变</td><td><code>score.structure_forecast</code></td><td>本地独立前瞻轨，可产生 forecast 推送候选</td><td>否，须单独过 forecast gate</td></tr>
	<tr><td>AI 触发</td><td><code>local_trigger</code></td><td>L0–L3 与指纹去重，决定本轮是否调用 AI</td><td>否</td></tr>
	<tr><td>AI 前瞻</td><td><code>analysis.parsed.forward_view</code></td><td>模型独立阅读行情事实，给出 horizon 内方向与概率</td><td>否，须校验、merge、post_audit</td></tr>
	<tr><td>权威结论</td><td><code>final_decision</code></td><td>AI 成功 / 未调用 / 失败三种场景下的唯一对外方向与 recommendation</td><td>仍须过 push_gate 与微信门禁</td></tr>
	<tr><td>推送分析</td><td><code>push_analysis</code></td><td>记录 would_push、阻断原因、候选轨道</td><td><code>would_push=true</code> 仍可能被冷却或缺少 SendKey 拦截</td></tr>
	</tbody></table>

	<div class="flow-chain"><span>collect_snapshot</span><span>detect_signals</span><span>score_snapshot</span><span>evaluate_ai_trigger</span><span>analyze_with_ai</span><span>merge_final_decision</span><span>post_audit</span><span>push_gate</span><span>微信门禁</span><span>Server酱</span></div>

	<h4>0.5.1 启用 AI 时的权威来源</h4>
	<table class="help-table"><thead><tr><th>场景</th><th><code>decision_source</code></th><th>对外方向</th></tr></thead><tbody>
	<tr><td>AI 返回有效 JSON</td><td><code>ai</code></td><td>优先 <code>forward_view.direction</code>，经 post_audit 后写入 final_decision</td></tr>
	<tr><td>AI 开启但本轮未调用</td><td><code>local_screening</code></td><td>保留 <code>score.final_direction</code>；trade 须达方向推送阈值 +5</td></tr>
	<tr><td>AI 调用失败或 JSON 无效</td><td><code>local_fallback</code></td><td>回退本地 final，同一高门槛规则</td></tr>
	<tr><td>AI 关闭</td><td><code>local_screening</code></td><td>完整本地模式；压测、模拟与推送均读本地 final</td></tr>
	</tbody></table>

	<h4>0.5.2 confirmed 与 forecast 双轨</h4>
	<ul class="help-list">
	<li><strong>confirmed</strong>：来自审计后的 <code>final_decision.push_recommendation</code>，类型可为 trade / spike / watch。</li>
	<li><strong>forecast</strong>：来自本地 <code>structure_forecast</code>，表示结构演变概率达到提醒线，不等于已确认结构单。</li>
	<li>两轨分别过 gate；微信层再提高门槛、按 trade → spike → forecast 选唯一候选，并执行同向冷却与同币种间隔。</li>
	</ul>

	<h4>0.5.3 压测页如何读图</h4>
	<ul class="help-list">
	<li><strong>蓝线</strong>：选定范围内该币种的价格轨迹。</li>
	<li><strong>绿线</strong>：按 <code>final_direction</code> 从 $10,000 满仓跟单的模拟权益。</li>
	<li><strong>点颜色</strong>：验证窗口成熟后，绿点=方向判定合理，红点=不合理，灰点=尚未到期。</li>
	<li><strong>价上方标记</strong>：该帧 <code>push_analysis.would_push=true</code> 的理论推送（◆急变 ■观察 △演变 ★结构单等）。</li>
	<li>范围可选「本次启动后 / 全部历史 / 回放会话」；测试页「导出诊断包」会打包 JSON、SVG/PNG 图与 runtime 全量日志。</li>
	</ul>
	<div class="help-note"><strong>排查建议：</strong>对照 JSONL 中的 <code>decision_source</code>、<code>local_trigger.should_call_ai</code>、<code>analysis.valid_json</code>、<code>post_audit</code> 与 <code>push_analysis</code>，区分「方向判断」「推送资格」「实际发送」三个问题。评分与 guard 细节见 §C、§G、§J。</div>

	<h4>0.5.4 微信层补充规则</h4>
	<ul class="help-list">
	<li>内部 <code>would_push</code> 不等于实际发微信；watch / spike / trade / forecast 各有额外分数门槛。</li>
	<li>每币种每轮最多一条；同向重复推送受冷却约束（含同趋势 leg 加长间隔）。</li>
	<li><code>push_enabled=false</code> 或缺少 SendKey 时只记 dry-run / skipped，不请求 Server酱。</li>
	</ul>
	</section>

		<section class="card help-card"><h2>§A 数据采集 collect_snapshot</h2>""",
        _mod(
            "A.1 原始行情与衍生品",
            "拉取 OKX 当前价、多周期 K 线、成交量、OI、资金费率、多空比、盘口；<strong>不判方向、不推送</strong>。可选写入 replay_dataset。",
            """<ul>
	<li>ticker、K 线（<code>BAR_CHANNELS</code>=1m/3m/5m/15m/1H/4H，各 <code>KLINE_LIMIT=200</code>）</li>
	<li><code>volume</code>：1m 当前量 / 近 20 根均量 → multiplier</li>
	<li>OI、费率按 ~60s 记入 history；<code>oi_change_pct_15m</code>、<code>funding_change</code> 需 <code>WARMUP_MINUTES=15</code></li>
	<li>盘口 top20 → imbalance、spread；缓存约 5s</li>
	</ul>""",
            """<ul>
	<li><code>volume.multiplier</code>：相对近期均量放大倍数，&gt;2 常意味异动</li>
	<li><code>oi_warmup_ready</code> / <code>funding_warmup_ready</code>：满 15 分钟才有意义的 15m 变化</li>
	<li>盘口 imbalance：买盘/卖盘堆积倾向，非方向结论</li>
	</ul>""",
            """<code>snapshot</code> 顶层：price, candles, volume, open_interest, funding_rate, funding_change, long_short_ratio, order_book, oi/funding_warmup 标志""",
            "§B 检测阈值；§A.2 算 profile；§A.3 算 market_context；AI payload 裁剪 K 线",
        ),
        _mod(
            "A.2 周期画像 trend_profiles（核心）",
            "对每个 K 线周期生成<strong>统一技术指标包 + trend 标签</strong>。回答「这一周期结构偏多/偏空/横盘/过渡」。<strong>策略方向、评分、演变、trade 票均读 profile，不读 score.trends。</strong>",
            """<p><strong>预处理</strong>：只使用已收盘 K 线（<code>confirmed=1</code>），下标 0 为最新一根；少于 5 根时标记为 <code>unknown</code>。指标负责描述市场，不单独构成买卖结论。</p>
	<div class="indicator-grid">
	<div class="indicator-card">
	<h5>3.1 EMA 指数移动平均线 <span>看趋势方向</span></h5>
	<p>越新的价格权重越大，因此比普通均线更快贴近当前价格。本系统计算 EMA9 / 20 / 60 / 120，并用 EMA 的排列判断短、中期趋势是否一致。</p>
	<p class="indicator-read"><strong>怎么看：</strong>价格 &gt; EMA9 &gt; EMA20 &gt; EMA60，且短期斜率向上，说明多头结构较整齐；反向排列则偏空。价格离 EMA20 越远，追涨杀跌风险通常越高。</p>
	<p class="indicator-note"><strong>注意：</strong>EMA 可理解为一段时间内的平均成本参考，不代表所有持仓者都盈利；震荡行情中价格会反复穿越均线。</p>
	</div>
	<div class="indicator-card">
	<h5>3.2 ATR 平均真实波幅 <span>看波动率</span></h5>
	<p>统计最近 14 根 K 线的平均真实波幅。真实波幅会同时考虑当根高低差，以及与上一根收盘价之间的跳空。</p>
	<p class="indicator-read"><strong>怎么看：</strong><code>atr</code> 是价格点数，例如 18；<code>atr_pct</code> 是波动占当前价格的比例，例如 0.08%。数值越大，行情越活跃，止损和目标距离也应相应放宽。</p>
	<p class="indicator-note"><strong>注意：</strong>ATR 只说明波动大不大，不说明上涨或下跌方向。</p>
	</div>
	<div class="indicator-card">
	<h5>3.3 结构高低点 <span>看关键价位</span></h5>
	<p>取当前 K 线之前最近 20 根已收盘 K 线的最高价和最低价，得到 <code>recent_high</code> 与 <code>recent_low</code>。</p>
	<p class="indicator-read"><strong>怎么看：</strong>收盘价上破结构高点记为 <code>breakout=up</code>，下破结构低点记为 <code>breakout=down</code>；这些位置也用于入场、止损和失效位参考。</p>
	<p class="indicator-note"><strong>注意：</strong>突破只代表价格越过关键位，仍需量能、趋势和收盘确认，防止假突破。</p>
	</div>
	<div class="indicator-card">
	<h5>3.4 RSI 相对强弱指数 <span>看多空力量</span></h5>
	<p>比较最近 N 根 K 线的平均上涨力度与平均下跌力度。本系统同时计算 RSI6 / 14 / 24：周期越短越灵敏，周期越长越平稳。</p>
	<p class="indicator-read"><strong>怎么看：</strong>50 附近表示多空力量相对平衡；高于 50 表示近期上涨力量更强，低于 50 表示下跌力量更强；70 / 30 常作为偏热、偏冷参考，系统的极端提醒使用 80 / 20。</p>
	<p><strong>背离：</strong>价格创新高而 RSI 未同步走高，称为顶背离，提示上涨动能衰减；价格创新低而 RSI 抬高，称为底背离，提示下跌动能衰减。</p>
	<p class="indicator-note"><strong>注意：</strong>强趋势中 RSI 可长时间停留在高位或低位，超买不等于立即下跌，超卖也不等于立即上涨。</p>
	</div>
	<div class="indicator-card">
	<h5>3.5 MACD <span>看趋势变化速度</span></h5>
	<p>比较短期 EMA 与长期 EMA，观察短期趋势相对长期趋势是在加速还是减速。本系统采用 12 / 26 / 9 参数。</p>
	<p class="indicator-read"><strong>DIF：</strong>EMA12 − EMA26；<strong>DEA：</strong>DIF 的 9 周期 EMA，用于平滑噪音；<strong>HIST：</strong>DIF − DEA，绝对值越大，当前方向的动能差越明显；<code>hist_slope</code> 表示柱体比上一根扩大还是收缩。</p>
	<p><strong>交叉：</strong>DIF 从下向上穿过 DEA 为金叉，表示上涨动能增强；从上向下穿过为死叉，表示下跌动能增强。价格创新高而 MACD 走弱属于顶背离，反之属于底背离。</p>
	<p class="indicator-note"><strong>注意：</strong>柱体颜色由图表软件定义，应以正负值和扩大/收缩为准；交叉在震荡区容易反复出现。</p>
	</div>
	<div class="indicator-card">
	<h5>3.6 KDJ 随机指标 <span>看区间位置</span></h5>
	<p>描述当前收盘价位于最近 9 根 K 线最高价与最低价区间中的相对位置，对短线拐点较敏感。</p>
	<p class="indicator-read"><strong>RSV：</strong>当前价格在近期高低区间中的百分比位置；<strong>K：</strong>对 RSV 平滑；<strong>D：</strong>再对 K 平滑；<strong>J：</strong><code>3K − 2D</code>，变化最灵敏，也可能超过 0–100。</p>
	<p><strong>怎么看：</strong>K &gt; D 表示短线位置转强，K &lt; D 表示转弱；高位和低位可用于观察过热、过冷及拐点。</p>
	<p class="indicator-note"><strong>注意：</strong>KDJ 与 RSI 都属于动量类指标，但计算逻辑不同；强趋势中容易高位或低位钝化，不能单独决定方向。</p>
	</div>
	<div class="indicator-card">
	<h5>3.7 BOLL 布林带 <span>看波动范围</span></h5>
	<p>以最近 20 根 K 线均价为中轨，上下各加减 2 倍标准差形成上轨和下轨。ATR 给出波动数值，BOLL 把波动范围直接画成价格通道。</p>
	<p class="indicator-read"><strong>怎么看：</strong><code>bandwidth_pct</code> 越小表示带宽收窄、行情压缩；带宽扩大表示波动释放。<code>position</code> 表示价格位于下轨到上轨之间的位置。</p>
	<p class="indicator-note"><strong>注意：</strong>触碰上轨不等于必跌，触碰下轨不等于必涨；趋势行情中价格可能沿轨运行。</p>
	</div>
	<div class="indicator-card">
	<h5>3.8 ADX 平均趋向指数 <span>看趋势强不强</span></h5>
	<p>衡量价格是否持续朝同一方向运动。连续的单向波动越明显，ADX 通常越高；来回震荡时 ADX 较低。</p>
	<p class="indicator-read"><strong>怎么看：</strong>ADX &lt; 20 通常表示趋势偏弱；约 25 以上可视为趋势开始有效；40 以上常见于强趋势。系统在 <code>ADX &lt; 18</code> 时优先把周期判为震荡。方向需结合 <code>+DI</code> 与 <code>-DI</code>：+DI 较高偏多，-DI 较高偏空。</p>
	<p class="indicator-note"><strong>注意：</strong>ADX 上升只表示趋势增强，不代表一定上涨；ADX 下降表示趋势降温，也不等于马上反转。</p>
	</div>
	</div>
	<p><strong>系统合成 trend 的顺序</strong>：① 收盘价 &gt; EMA9 &gt; EMA20 &gt; EMA60 且短期斜率向上 → <code>up</code>；② 反向排列且斜率向下 → <code>down</code>；③ ADX&lt;18、斜率绝对值&lt;0.08% 或 ATR%&lt;0.08% → <code>range</code>；④ 其余过渡结构 → <code>mixed</code>。</p>""",
            """<table class="help-table"><thead><tr><th>字段/标签</th><th>怎么理解</th><th>不能单独说明什么</th></tr></thead><tbody>
	<tr><td><code>trend=up/down</code></td><td>均线排列与短斜率同向，结构趋势明确</td><td>不代表当前价位适合立即追单</td></tr>
	<tr><td><code>range</code></td><td>无明显趋势或波动极弱，不宜追方向</td><td>不代表完全没有短线机会</td></tr>
	<tr><td><code>mixed</code></td><td>过渡态，例如价格反弹但 EMA 尚未理顺</td><td>不等于随机行情，可能正在形成拐点</td></tr>
	<tr><td><code>breakout</code></td><td>价格突破近期结构高点或低点</td><td>不保证是真突破，仍需量能与收盘确认</td></tr>
	<tr><td><code>distance_to_ema20_atr</code></td><td>当前价距离 EMA20 有多少倍 ATR</td><td>只衡量偏离程度，不直接判断涨跌</td></tr>
	<tr><td><code>data_quality.is_reliable</code></td><td>至少 35 根已确认 K 线，指标才视为较可靠</td><td>数据不足时所有指标都应降权</td></tr>
	</tbody></table>
	<p><code>score.trends[bar]</code> 仅为 5 根首尾对比的<strong>展示兼容</strong>，策略不读。</p>""",
            """<code>snapshot.trend_profiles[bar]</code>：trend, breakout, divergence, ema, atr, rsi, macd, kdj, boll, adx, recent_high/low, body_ratio, data_quality 等""",
            """<ul>
	<li><code>market_context.trade_up/down</code>：5m+15m trend 投票</li>
	<li>§B <code>structure_break</code>、§C 八层 trend/momentum 层</li>
	<li>§D raw_direction（5m/15m/1H/4H 组合）</li>
	<li>§C 演变 mixed_to_up、profile_lag 等场景</li>
	</ul>""",
        ),
        _mod(
            "A.3 市场语境 market_context",
            "把当前策略相关周期的趋势画像、动态价格压力、ATR 波动、成交量、OI、资金费率、多空比和盘口，整理成一份<strong>市场状态说明书</strong>。它回答三个问题：① 当前是趋势、震荡、压缩还是高波动；② 结构暂时偏多、偏空还是中性；③ 哪些力量正在支持或反对该结构。<strong>它不是最终买卖结论，也不直接决定推送。</strong>",
            """<p><strong>计算分两阶段：</strong>采集快照时先执行 1–8，生成基础 <code>market_context</code>；进入 <code>score_snapshot</code> 后再执行第 9 步，追加 <code>sentiment_meta</code>。</p>
	<h4>第 1 步：按策略把相关周期分成三组投票</h4>
	<table class="help-table"><thead><tr><th>分组</th><th>读取周期</th><th>作用</th></tr></thead><tbody>
	<tr><td><code>entry</code></td><td>随策略切换：1m/3m、15m 或 4H</td><td>观察入场级别是否开始转向</td></tr>
	<tr><td><code>trade</code></td><td>随策略切换：3m/5m、5m/15m、1H/4H 或 1D</td><td>当前策略的主方向结构</td></tr>
	<tr><td><code>higher</code></td><td>随策略切换：15m/1H、1H/4H、4H/1D 或 1W</td><td>上级背景，防止逆大级别追单</td></tr>
	</tbody></table>
	<p>每个周期读取 <code>trend_profiles[周期].trend</code>，先在 entry/trade/higher 组内计算方向占比，再乘策略权重。最终同时保留兼容计数和 <code>trend_vote_metrics</code> 加权比例，避免不同策略票数不同却共用固定“两票/三票”门槛。</p>

	<h4>第 2 步：按策略计算价格压力窗口</h4>
	<p>超短/短线使用 1m 数据计算 5/10/15/20 分钟；中线使用 15m 数据计算 15/30/45/60 分钟；长线使用 4H 数据计算 4/8/12/24 小时。真实语义写入 <code>pressure_windows</code>，旧 <code>recent_move_pct</code> 仅作兼容。</p>

	<h4>第 3 步：把短窗涨跌换成价格压力</h4>
	<table class="help-table"><thead><tr><th>观察窗口</th><th>达到该幅度才计票</th></tr></thead><tbody>
	<tr><td>5m</td><td><code>max(0.08%, 15m ATR% × 0.30)</code></td></tr>
	<tr><td>10m</td><td><code>max(0.14%, 15m ATR% × 0.45)</code></td></tr>
	<tr><td>15m</td><td><code>max(0.20%, 15m ATR% × 0.60)</code></td></tr>
	</tbody></table>
	<p>某窗口达到阈值后，上涨记一张 <code>up</code> 压力票，下跌记一张 <code>down</code> 压力票。满足以下任一条件即输出方向压力：</p>
	<ul>
	<li><code>down</code>：至少两张下跌票，或 5m 跌幅 ≤ <code>-max(0.12%, ATR% × 0.35)</code>；</li>
	<li><code>up</code>：至少两张上涨票，或 5m 涨幅 ≥ <code>max(0.12%, ATR% × 0.35)</code>；</li>
	<li>否则为 <code>neutral</code>。</li>
	</ul>
	<p>因此 <code>recent_price_pressure</code> 表示<strong>最近几分钟价格实际正在向哪边用力</strong>，不是长期趋势。</p>

	<h4>第 4 步：判断结构是否已经确认</h4>
	<p><strong>偏多确认 <code>long_confirmed</code></strong> 同时要求：</p>
	<ul>
	<li>higher 组方向占比至少 50%；</li>
	<li>策略加权后的多头值大于空头值；</li>
	<li>trade 组方向占比至少 75%，或达到 50% 并同时获得同向压力和 entry 确认。</li>
	</ul>
	<p><strong>偏空确认 <code>short_confirmed</code></strong> 完全对称：大周期至少一张空票、总空票更多，并由 5m/15m 双空或“交易周期一张空票 + 短窗向下 + 入场周期一张空票”确认。</p>

	<h4>第 5 步：按优先级合成 bias 与 regime</h4>
	<table class="help-table"><thead><tr><th>判断顺序</th><th>条件</th><th>输出</th></tr></thead><tbody>
	<tr><td>1. 压缩</td><td>15m 布林带宽 &lt; <code>max(0.35%, ATR% × 1.4)</code>，且 ADX&lt;18</td><td><code>bias=neutral</code>，<code>regime=squeeze</code></td></tr>
	<tr><td>2. 多头趋势</td><td><code>long_confirmed=true</code></td><td><code>bias=long</code>，<code>regime=trend_up</code></td></tr>
	<tr><td>3. 空头趋势</td><td><code>short_confirmed=true</code></td><td><code>bias=short</code>，<code>regime=trend_down</code></td></tr>
	<tr><td>4. 震荡</td><td>策略加权后的 range/mixed 比例至少 50%</td><td><code>bias=neutral</code>，<code>regime=range</code></td></tr>
	<tr><td>5. 过渡</td><td>以上均不满足</td><td><code>bias=neutral</code>，<code>regime=mixed</code></td></tr>
	</tbody></table>

	<h4>第 6 步：执行三层覆盖与降级</h4>
	<ol class="help-list">
	<li>若策略 regime 周期的 ATR% 达到同周期近期历史 P80，<code>regime</code> 覆盖为 <code>high_volatility</code>。</li>
	<li>若原本是 <code>trend_up/down</code>，但策略 regime 周期 ADX&lt;16，趋势强度不足，降为 <code>mixed</code>。</li>
	<li>若结构与战术压力反向，设置 <code>bias_softened=true</code>，保留 <code>structural_bias</code>，并生成 <code>pullback_in_uptrend</code> 或 <code>rebound_in_downtrend</code> 等 <code>trend_phase</code>。</li>
	</ol>

	<h4>第 7 步：价量与持仓状态</h4>
	<p><code>oi_price_state</code> 把价格变化和 15 分钟 OI 变化放在一起解释。OI 变化绝对值小于 0.5% 时直接记为 <code>oi_flat</code>；否则按“价涨/价跌 × OI增/OI减”分为四类。</p>
	<div class="help-note"><strong>当前实现口径提醒：</strong><code>price_change_15m</code> 这个字段名容易误解。代码实际用“当前价”和约 4 根前的 15m 收盘价比较，正常数据下约覆盖 60 分钟；OI 仍是最近 15 分钟变化。阅读日志时应按这个真实口径理解。</div>

	<h4>第 8 步：动态阈值、盘口和风险警告</h4>
	<ul>
	<li>每个币种约每分钟保存一次成交量倍数、15m ATR% 和盘口不平衡，保留约 180 分钟历史。</li>
	<li><code>volume_threshold_used = max(用户配置 volume_multiplier, 近期成交量倍数 P85)</code>。</li>
	<li>盘口方向使用 <code>(top20 imbalance + top5 imbalance) ÷ 2</code>。绝对门槛为 <code>max(0.25, 近期盘口绝对不平衡 P85 × 0.8)</code>；向上超过门槛为 <code>bid_support</code>，向下超过门槛为 <code>ask_pressure</code>。</li>
	<li><code>warnings</code> 汇总布林压缩、高 ATR、弱 ADX、RSI 背离/极端、MACD 动能放缓、费率过热、多空拥挤及突破未放量等风险。</li>
	</ul>

	<h4>第 9 步：评分阶段追加 sentiment_meta</h4>
	<p>情绪层分别累计多头分和空头分。只有一侧至少 3 分，且领先另一侧至少 1 分，才输出做多或做空；否则观望。</p>
	<table class="help-table"><thead><tr><th>因素</th><th>多头分</th><th>空头分</th></tr></thead><tbody>
	<tr><td>价涨 + OI增 / 价跌 + OI增</td><td>价涨 OI增：+2</td><td>价跌 OI增：+2</td></tr>
	<tr><td>价涨 + OI减 / 价跌 + OI减</td><td>空头回补：+1</td><td>多头去杠杆：+1</td></tr>
	<tr><td>OI 已预热、变化≥2%，且价格明显同向</td><td>价格&gt;+0.05%：+1</td><td>价格&lt;-0.05%：+1</td></tr>
	<tr><td>资金费率 15m 变化达到阈值</td><td>费率下降：+1</td><td>费率上升：+1</td></tr>
	<tr><td>多空账户拥挤</td><td>空头占比达到极端值：+1</td><td>多头占比达到极端值：+1</td></tr>
	<tr><td>盘口方向</td><td><code>bid_support</code>：+1</td><td><code>ask_pressure</code>：+1</td></tr>
	</tbody></table>""",
            """<table class="help-table"><thead><tr><th>字段</th><th>通俗含义</th><th>使用限制</th></tr></thead><tbody>
	<tr><td><code>regime</code></td><td>当前市场形态：趋势、震荡、压缩、过渡或高波动</td><td>描述“环境”，不是买卖方向</td></tr>
	<tr><td><code>bias</code></td><td>多周期结构倾向：<code>long/short/neutral</code></td><td>偏多不等于现在就该追多</td></tr>
	<tr><td><code>bias_softened</code></td><td>大结构与最近几分钟的价格压力发生冲突</td><td>表示需要降级确认，不等于大趋势已经反转</td></tr>
	<tr><td><code>trend_votes</code></td><td>1m/3m、5m/15m、1H/4H 的原始趋势标签</td><td>应结合分组看，不能只数总票数</td></tr>
	<tr><td><code>entry_up/down</code></td><td>1m、3m 中有几个周期偏多/偏空</td><td>灵敏但噪音大，只做入场确认</td></tr>
	<tr><td><code>trade_up/down</code></td><td>5m、15m 中有几个周期偏多/偏空</td><td>短线方向确认的核心票，但仍受压力和风控约束</td></tr>
	<tr><td><code>recent_price_pressure</code></td><td>最近 5–15 分钟价格实际向上、向下或中性</td><td>只看短窗，不代表 1H/4H 趋势</td></tr>
	<tr><td><code>recent_move_pct</code></td><td>1m K 线计算的 5/10/15/20 分钟涨跌幅</td><td>是百分比，不是 ATR 倍数</td></tr>
	<tr><td><code>oi_price_state</code></td><td>价格和持仓量组合后的行为标签</td><td>只能给出“新仓/平仓压力”的可能解释，不能识别具体是谁开仓</td></tr>
	<tr><td><code>order_book_bias</code></td><td>盘口短线买单支撑、卖单压力或中性</td><td>盘口撤单很快，只作确认，不单独定方向</td></tr>
	<tr><td><code>volume_threshold_used</code></td><td>本轮判定放量实际采用的阈值</td><td>取用户阈值和近期 P85 中较高者，因此会随市场变化</td></tr>
	<tr><td><code>sentiment_meta</code></td><td>OI、费率、拥挤度和盘口合成的领先情绪方向及强度</td><td>在 <code>score_snapshot</code> 阶段才追加；若与 5m/15m 强趋势相反，不会直接带方向</td></tr>
	<tr><td><code>strategy_template</code></td><td>当前环境对应的策略提示模板</td><td>是策略分类，不是自动下单指令</td></tr>
	<tr><td><code>warnings</code></td><td>本轮需要特别防范的风险列表</td><td>一个警告通常只触发降权或等待确认</td></tr>
	</tbody></table>
	<h4>regime 对应的策略模板</h4>
	<table class="help-table"><thead><tr><th>市场状态</th><th>strategy_template</th><th>直观解释</th></tr></thead><tbody>
	<tr><td><code>trend_up + long</code></td><td><code>trend_pullback_long</code></td><td>上升趋势中等回踩，而不是追离均线很远的位置</td></tr>
	<tr><td><code>trend_down + short</code></td><td><code>trend_pullback_short</code></td><td>下降趋势中等反弹后再确认</td></tr>
	<tr><td><code>squeeze</code></td><td><code>wait_breakout_after_squeeze</code></td><td>波动压缩，等待放量选择方向</td></tr>
	<tr><td><code>range</code></td><td><code>range_edge_only</code></td><td>只考虑区间边缘，不在中间追涨杀跌</td></tr>
	<tr><td><code>high_volatility</code></td><td><code>reduce_size_wait_retest</code></td><td>波动过大，降低仓位并等待回踩/反抽确认</td></tr>
	<tr><td>其它</td><td><code>no_trade_until_alignment</code></td><td>结构尚未对齐，继续观察</td></tr>
	</tbody></table>""",
            """<p><strong>基础输出：</strong><code>snapshot.market_context</code> 包含 regime、bias、bias_softened、trend_votes、各组票数、recent_price_pressure、recent_move_pct、price_change_15m、oi_price_state、volume_threshold_used、order_book_bias、strategy_template、warnings。</p>
	<p><strong>并列输出：</strong><code>snapshot.volatility</code> 保存 ATR 与波动状态；<code>snapshot.dynamic_thresholds</code> 保存 P80/P85/P95、自适应阈值样本数和币种 fallback 参数。</p>
	<p><strong>评分阶段补充：</strong><code>score_snapshot</code> 会把 <code>sentiment_meta</code> 写回 <code>snapshot.market_context</code>，同时复制到 <code>score.sentiment_meta</code>。</p>""",
            """<ul>
	<li><strong>信号检测：</strong><code>regime=squeeze</code> 触发布林挤压观察信号；动态放量和盘口门槛决定相应信号是否成立。</li>
	<li><strong>八层评分：</strong>regime 决定市场环境分；pressure 与目标方向相反会扣趋势、动量、入场和风控分；OI、盘口和 sentiment 分别进入衍生品、盘口层。</li>
	<li><strong>方向 guard：</strong>做多遇到 <code>pressure=down</code>、做空遇到 <code>pressure=up</code> 时，按策略周期和风险偏好拦截或要求更多 5m/15m 票。</li>
	<li><strong>策略方向：</strong>价格结构尚未确认时，足够强的 <code>sentiment_meta</code> 可提供领先方向；但若 5m 与 15m 同时强烈反向，则不会采用。</li>
	<li><strong>入场计划：</strong>range、mixed、high_volatility 通常降为等待确认；盘口不支持目标方向时也会降低入场质量。</li>
	<li><strong>推送复核：</strong>AI 给出的 trade 若与短窗压力、scalp 方向或结构状态严重冲突，可被 post-audit 降级。</li>
	<li><strong>结构演变：</strong>mixed、squeeze、短窗压力和 MACD 变化共同参与“趋势正在形成”的演变判断。</li>
	</ul>""",
        ),
        """
	</section>

	<section class="card help-card"><h2>§B 信号检测 detect_signals</h2>""",
        _mod(
            "B 阈值信号（11 类）",
            "对同一份市场快照依次检查 11 类异常条件，把所有命中的条件放进 <code>signals[]</code>。它回答的是<strong>「现在发生了哪些值得进一步分析的事」</strong>，不是「应该做多还是做空」。同一轮可以命中多个信号；检测过程不读取交易、观察、急变推送开关，因此<strong>关掉某类推送不会停止信号检测</strong>。",
            """<h4>整体执行顺序</h4>
	<div class="flow-chain"><span>读取 snapshot</span><span>算动态门槛</span><span>逐项检查 11 类信号</span><span>全部命中项写入 signals[]</span><span>评分</span><span>L0–L3 触发判断</span><span>AI/推送复核</span></div>
	<p>函数不会“命中一个就停止”，而是按照下表顺序继续检查，因此一轮可能同时出现放量、突破、MACD、OI、盘口等多条信号。</p>

	<h4>输入数据从哪里来</h4>
	<table class="help-table"><thead><tr><th>输入</th><th>读取字段</th><th>用途</th></tr></thead><tbody>
	<tr><td>1m 成交量</td><td><code>snapshot.volume</code></td><td>放量倍数及强度</td></tr>
	<tr><td>技术指标</td><td><code>trend_profiles["5m"/"15m"]</code></td><td>结构突破、布林、RSI、MACD、ADX</td></tr>
	<tr><td>市场语境</td><td><code>market_context</code></td><td>squeeze、OI 价格组合、盘口方向说明</td></tr>
	<tr><td>合约数据</td><td>OI、资金费率、多空账户比</td><td>持仓异动、费率过热、拥挤度</td></tr>
	<tr><td>订单簿</td><td><code>order_book</code></td><td>top20 买卖盘不平衡</td></tr>
	<tr><td>动态阈值</td><td><code>dynamic_thresholds</code></td><td>根据近期市场活跃度自动抬高放量和盘口门槛</td></tr>
	</tbody></table>
	<div class="help-note">动态 P85/P95 使用此前约 180 分钟、约每分钟一条的历史样本。本轮最新成交量、ATR 和盘口值是在 <code>detect_signals</code> 所需上下文计算完成后才写入历史，因此不会用“当前异常值”抬高自己本轮的触发门槛。</div>

	<h4>1. volume_spike — 已收盘 1m 放量</h4>
	<p><strong>基础值：</strong>最新一根已收盘 1m K 线成交量 ÷ 前 20 根已收盘 1m K 线平均成交量，得到 <code>volume.multiplier</code>。</p>
	<p><strong>触发门槛：</strong><code>multiplier ≥ max(用户配置 volume_multiplier, 近期成交量倍数 P85)</code>。默认用户值为 2.0 倍；高活跃时段若近期 P85 更高，就自动采用更高值，减少普通放量误报。</p>
	<p><strong>强度：</strong>达到近期 P95 时写入 <code>strength=high</code>，否则为 <code>normal</code>。没有历史样本时，P95 fallback 约为当前触发门槛的 1.5 倍。</p>
	<p><strong>含义：</strong>交易活跃度突然提高。它本身没有方向；方向要再看该 1m K 线涨跌、结构突破、OI 和盘口。</p>

	<h4>2. structure_break — 5m / 15m 结构突破</h4>
	<p>只要 <code>5m.breakout</code> 或 <code>15m.breakout</code> 为 <code>up/down</code> 就触发。breakout 的来源是：最新收盘价突破此前 20 根已收盘 K 线的结构最高价或最低价。</p>
	<p><strong>方向提示：</strong>任一周期包含 <code>up</code> 时写 <code>direction_hint=做多</code>，否则写做空。它只是提示，不是最终方向。</p>
	<div class="help-note"><strong>冲突口径：</strong>如果 5m 向上突破、15m 向下突破，当前代码仍会给“做多”提示，因为判断规则是“只要其中有 up 就提示做多”。后续多周期评分、AI 和冲突复核仍会看到两个周期的原始 breakout 值。</div>

	<h4>3. boll_squeeze — 15m 波动压缩</h4>
	<p>当 <code>market_context.regime=squeeze</code> 时触发。其上游条件是：15m 布林带宽小于 <code>max(0.35%, 15m ATR% × 1.4)</code>，并且 15m ADX&lt;18。</p>
	<p><strong>含义：</strong>波动收窄且趋势较弱，市场可能在蓄势。该信号没有突破方向，通常表示等待后续放量选择方向。</p>

	<h4>4. rsi_divergence — 15m RSI 背离</h4>
	<p>当 <code>trend_profiles["15m"].divergence</code> 为 <code>bearish</code> 或 <code>bullish</code> 时触发。</p>
	<ul>
	<li><code>bearish</code>：价格高于此前参考高点，但当前 RSI14 比参考 RSI 低至少 3，提示上涨动能没有跟上；</li>
	<li><code>bullish</code>：价格低于此前参考低点，但当前 RSI14 比参考 RSI 高至少 3，提示下跌动能没有跟上。</li>
	</ul>
	<p><strong>注意：</strong>这是风险/反转观察信号，不代表背离出现后价格会立即反转。</p>

	<h4>5. rsi_extreme — 15m RSI 极端</h4>
	<p>15m RSI14 ≥ 80 或 ≤ 20 时触发。这里比常见的 70/30 更严格，目的是只提醒更极端的动量状态。</p>
	<p><strong>含义：</strong>≥80 表示近期上涨力量非常集中，≤20 表示下跌力量非常集中；既可能是趋势很强，也可能意味着追单风险变高，因此该信号不直接给反向方向。</p>

	<h4>6. macd_momentum_change — 15m MACD 柱体变化明显</h4>
	<p>同时满足：</p>
	<ul>
	<li><code>|hist_slope| &gt; |hist| × 0.25</code>；</li>
	<li><code>|hist| &gt; 0</code>。</li>
	</ul>
	<p><code>hist_slope = 当前 HIST − 上一根 HIST</code>。也就是柱体相对当前柱值变化超过 25% 时触发。</p>
	<table class="help-table"><thead><tr><th>HIST</th><th>hist_slope</th><th>直观理解</th></tr></thead><tbody>
	<tr><td>正</td><td>正</td><td>上涨动能柱继续扩大</td></tr>
	<tr><td>正</td><td>负</td><td>上涨动能仍为正，但正在减弱</td></tr>
	<tr><td>负</td><td>负</td><td>下跌动能柱继续扩大</td></tr>
	<tr><td>负</td><td>正</td><td>下跌动能仍为负，但正在减弱</td></tr>
	</tbody></table>
	<p><strong>注意：</strong>检测函数只标记“变化明显”，不根据正负直接写方向提示；方向由后续动量评分判断。当前规则没有设置最小绝对柱值门槛，HIST 非零但非常接近 0 时，较小的绝对变化也可能满足“相对变化 25%”。</p>

	<h4>7. oi_change — 15 分钟持仓量异动</h4>
	<p>必须同时满足：</p>
	<ul>
	<li><code>oi_warmup_ready=true</code>：系统已积累至少约 15 分钟 OI 历史；</li>
	<li><code>|oi_change_pct_15m| ≥ oi_change_pct_15m 配置</code>，默认 5%。</li>
	</ul>
	<p><strong>含义：</strong>合约总持仓显著增加或减少。描述中会附带 <code>oi_price_state</code>，用价格变化配合解释新多、新空、空头回补或多头去杠杆的可能性。</p>
	<p><strong>注意：</strong>OI 增加本身不等于做多，OI 减少也不等于做空；未完成预热时，即使数值变化很大也不触发。</p>

	<h4>8. funding_hot — 当前资金费率过热</h4>
	<p><code>|funding_rate| ≥ funding_abs_threshold</code> 时触发，默认阈值为 0.0008，即页面百分比口径约 0.08%。此信号<strong>不需要预热</strong>。</p>
	<p><strong>含义：</strong>资金费率绝对值过大，说明一侧持仓较拥挤、持仓成本较高。正费率通常表示多头向空头付费，负费率通常相反；它更像拥挤风险提示，而不是顺势信号。</p>

	<h4>9. funding_fast_change — 15 分钟资金费率快速变化</h4>
	<p>必须完成约 15 分钟资金费率预热，并满足 <code>|funding_change| ≥ funding_change_threshold</code>。默认 0.0003，约等于费率变化 0.03 个百分点。</p>
	<p><strong>含义：</strong>合约情绪或拥挤度正在快速改变。正向变化和负向变化都会触发，但检测函数不直接给方向；情绪积分层会把费率上行偏向空头风险、费率下行偏向多头风险释放。</p>

	<h4>10. long_short_extreme — 多空账户占比极端</h4>
	<p>多头账户占比 ≥ <code>long_short_extreme</code> 时触发；否则再检查空头账户占比。默认阈值为 0.75，即 75%。</p>
	<p><strong>含义：</strong>某一侧参与者过于集中，提示拥挤和反向挤压风险。多头占比高不是继续做多信号，空头占比高也不是继续做空信号。</p>
	<p><strong>实现细节：</strong>这里使用 <code>if / elif</code>，一轮最多写入一条多空极端信号。</p>

	<h4>11. order_book_imbalance — top20 盘口不平衡</h4>
	<p>先要求订单簿数据可用，再计算：</p>
	<p><code>imbalance = (top20 买单量 − top20 卖单量) ÷ (top20 买单量 + top20 卖单量)</code></p>
	<p>当 <code>|imbalance| ≥ max(0.35, 近期 |imbalance| P85)</code> 时触发。正值表示买单量更多，负值表示卖单量更多。</p>
	<p><strong>注意：</strong>信号触发使用 top20 原始 imbalance；描述中的 <code>order_book_bias</code> 则由 top5 与 top20 的平均值及另一套较低门槛计算，所以少数情况下可能出现“已触发盘口异动，但 bias 仍为 neutral”。盘口也可能快速撤单，只能做短线确认。</p>

	<h4>默认阈值与预热要求汇总</h4>
	<table class="help-table"><thead><tr><th>信号</th><th>默认/固定门槛</th><th>动态门槛</th><th>需 15m 预热</th></tr></thead><tbody>
	<tr><td>volume_spike</td><td>用户值默认 2.0x</td><td>与近期 P85 取较高值；P95 判 high</td><td>否</td></tr>
	<tr><td>structure_break</td><td>突破前 20 根结构位</td><td>无</td><td>否</td></tr>
	<tr><td>boll_squeeze</td><td>ADX&lt;18 + 带宽条件</td><td>带宽参考 ATR%</td><td>否</td></tr>
	<tr><td>rsi_divergence</td><td>RSI 差至少 3</td><td>无</td><td>否</td></tr>
	<tr><td>rsi_extreme</td><td>RSI≥80 或 ≤20</td><td>无</td><td>否</td></tr>
	<tr><td>macd_momentum_change</td><td>柱体相对变化&gt;25%</td><td>无</td><td>否</td></tr>
	<tr><td>oi_change</td><td>默认 |变化|≥5%</td><td>无</td><td><strong>是</strong></td></tr>
	<tr><td>funding_hot</td><td>默认 |费率|≥0.0008</td><td>无</td><td>否</td></tr>
	<tr><td>funding_fast_change</td><td>默认 |变化|≥0.0003</td><td>无</td><td><strong>是</strong></td></tr>
	<tr><td>long_short_extreme</td><td>任一侧≥75%</td><td>无</td><td>否</td></tr>
	<tr><td>order_book_imbalance</td><td>|值|≥0.35</td><td>与近期 P85 取较高值</td><td>否</td></tr>
	</tbody></table>""",
            """<h4>先看信号属于哪一类</h4>
	<table class="help-table"><thead><tr><th>分类</th><th>包含信号</th><th>系统如何理解</th></tr></thead><tbody>
	<tr><td><code>TRADE_TRIGGER_SIGNALS</code></td><td>volume_spike、structure_break、oi_change、order_book_imbalance、macd_momentum_change</td><td>与价格行为、资金跟随或动量变化更直接相关，可参与 L2 交易型分析，但仍不等于可交易</td></tr>
	<tr><td><code>WATCH_TRIGGER_SIGNALS</code></td><td>funding_hot、funding_fast_change、rsi_extreme、rsi_divergence、boll_squeeze、long_short_extreme</td><td>更偏风险、蓄势、极端和反转观察，方向不够确定时可形成 watch</td></tr>
	</tbody></table>

	<h4>哪些信号带方向</h4>
	<table class="help-table"><thead><tr><th>信号</th><th>检测阶段是否给方向</th><th>正确读法</th></tr></thead><tbody>
	<tr><td>structure_break</td><td>是，写 <code>direction_hint</code></td><td>只是突破方向提示，仍需趋势、量能和压力确认</td></tr>
	<tr><td>volume_spike</td><td>否</td><td>只说明活跃度上升</td></tr>
	<tr><td>MACD / OI / 资金费率 / 多空比 / RSI / BOLL / 盘口</td><td>检测对象可能有正负或多空含义，但 signal 本身不写最终方向</td><td>由 <code>market_context</code>、八层评分和 AI 综合解释</td></tr>
	</tbody></table>

	<h4>最容易误读的地方</h4>
	<ul>
	<li><strong>有信号 ≠ 可以下单：</strong>signal 只是异常事件清单，方向、入场质量和风险仍可能全部不合格。</li>
	<li><strong>信号数量 ≠ 胜率：</strong>两条同源风险信号不一定比一条高质量结构突破更可靠。</li>
	<li><strong>观察信号不一定看空：</strong>RSI 极端、资金费率过热和拥挤度只提示风险，强趋势可以继续延伸。</li>
	<li><strong>盘口和成交量没有天然方向：</strong>必须结合 K 线方向、结构和持续性。</li>
	<li><strong>预热标志很重要：</strong>OI 与费率变化在刚启动时没有完整 15 分钟基准，因此系统主动禁止触发。</li>
	</ul>""",
            """<p>返回一个列表：<code>signals[]</code>。没有任何命中时返回空列表。</p>
	<table class="help-table"><thead><tr><th>字段</th><th>何时出现</th><th>含义</th></tr></thead><tbody>
	<tr><td><code>type</code></td><td>每条都有</td><td>稳定的机器可读信号类型</td></tr>
	<tr><td><code>desc</code></td><td>每条都有</td><td>本轮实际数值、门槛或上下文说明，主要用于日志和 AI payload</td></tr>
	<tr><td><code>strength</code></td><td>仅 volume_spike</td><td><code>normal/high</code>，high 表示达到近期 P95</td></tr>
	<tr><td><code>direction_hint</code></td><td>仅 structure_break</td><td>突破方向提示：做多或做空</td></tr>
	</tbody></table>
	<p>示例：<code>{"type":"volume_spike","desc":"confirmed 1m volume multiplier 2.40x >= 2.10x","strength":"normal"}</code></p>""",
            """<h4>从 signals 到 AI 与推送的完整关系</h4>
	<ol class="help-list">
	<li><strong>无信号：</strong><code>evaluate_ai_trigger</code> 直接输出 L0，不调用 AI。</li>
	<li><strong>有一条普通信号：</strong>默认从 L1 开始，通常只做本地筛查。</li>
	<li><strong>至少两条信号：</strong>升级候选 L2，原因记为 <code>multi_signal</code>。</li>
	<li><strong>单条交易类信号：</strong>若是放量、结构突破、OI 或盘口，可满足 <code>trade_signal</code> L2；若只有 MACD，默认还需另一条信号。配置 <code>l2_require_volume_or_structure=false</code> 时限制会放宽。</li>
	<li><strong>多条观察信号：</strong>WATCH 集合命中至少两类时可升 L2，原因记为 <code>multi_watch</code>。</li>
	<li><strong>情绪信号：</strong>OI 快变、费率快变或多空极端，配合 <code>sentiment_meta.strength≥2</code> 可升 L2。</li>
	<li><strong>L3：</strong>不是由 11 类信号数量直接决定；scalp 急变达到分数，或 funding_hot 达到基础阈值的 1.25 倍时进入 L3。</li>
	<li><strong>八层评分：</strong>structure_break 给趋势层加分；volume_spike 给量价层加分；无放量的突破会被扣分；OI、费率、拥挤度和盘口进入各自评分层。</li>
	<li><strong>最终推送：</strong>trade/watch/spike 仍要经过 AI 或本地合并、置信度门槛、方向冲突复核和推送开关。检测到 WATCH 信号只是让“观察推送”具备候选资格。</li>
	</ol>""",
        ),
        """
	</section>

	<section class="card help-card"><h2>§C 本地评分 score_snapshot</h2>""",
        _mod(
            "C 八层评分 + 演变轨",
            "把市场环境、趋势、动量、量价、合约资金、盘口、入场质量和风险控制分别评分，再合成为 0–100 的<strong>本地观察强度</strong>。评分用于筛查“值不值得继续分析”和解释强弱，不是自动下单概率，也不直接等于最终推送置信度。",
            """<h4>完整执行顺序</h4>
	<div class="flow-chain"><span>计算 sentiment_meta</span><span>选 raw_direction</span><span>八层评分</span><span>direction_guard</span><span>生成入场计划</span><span>低质量降级</span><span>三策略视图</span><span>选中视图覆盖</span><span>结构演变</span><span>在线校准</span></div>
	<ol class="help-list">
	<li>根据当前 <code>strategy_mode</code> 先得到 <code>raw_direction</code>。</li>
	<li>所有层都围绕该方向计算；若方向为观望，方向性加分自然减少。</li>
	<li>八层分别加减分、应用策略权重，再各自截断到层上限。</li>
	<li>八层相加得到 <code>raw_total_score</code>，范围 0–100。</li>
	<li><code>direction_guard</code> 检查短窗压力、5m/15m 票和风险偏好；命中则先把方向降为观望。</li>
	<li><code>_suggest_levels</code> 生成入场、止损、止盈及等待条件；质量不足且分数不够时再次降级。</li>
	<li>同时计算 scalp / short / swing 三个策略视图；当前选中的策略视图可在无 guard 时覆盖本地方向和交易分。</li>
	<li>最后独立计算 <code>structure_forecast</code>，它预测结构演变，但不直接修改 <code>final_decision</code>。</li>
	</ol>

	<h4>八层评分明细</h4>
	<table class="help-table"><thead><tr><th>层</th><th>基础/上限</th><th>主要加分</th><th>主要扣分</th></tr></thead><tbody>
	<tr><td>市场状态</td><td>基础 6 / 上限 12</td><td>trend_up/down +8；squeeze +4</td><td>range/mixed −2；high_volatility −4</td></tr>
	<tr><td>趋势</td><td>基础 8 / 上限 16</td><td>15m 与 1H 同向 +5；ADX≥20 且 DI 支持方向 +4；结构突破 +3；1H/4H 同向再加权</td><td>range/mixed 或 ADX&lt;16 −4；数据不足 −3；短窗压力反向 −5；高周期反向</td></tr>
	<tr><td>动量</td><td>基础 6 / 上限 12</td><td>做多 RSI 50–72 / 做空 28–50 +3；MACD 同向且柱体增强 +4；5m KDJ 同向 +2</td><td>RSI&gt;80 或 &lt;20 −4；背离 −3；指标未就绪 −2；压力反向 −4</td></tr>
	<tr><td>量价</td><td>基础 5 / 上限 12</td><td>放量 +5；最新 1m K 线方向一致 +2；近 5 根均量上升 +2</td><td>突破但未放量 −3；压力反向 −2</td></tr>
	<tr><td>合约资金</td><td>基础 6 / 上限 14</td><td>价格与 OI 同向增仓 +4；OI 预热后变化≥2% +2；情绪同向最多 +3；费率变化支持方向 +2</td><td>回补/去杠杆型走势 −2；费率过热 −3；拥挤与方向冲突 −3，同向仍 −1</td></tr>
	<tr><td>盘口</td><td>基础 4 / 上限 8</td><td>目标方向获得 bid_support / ask_pressure +3</td><td>top5 比 top20 极端过多 −1；价差&gt;0.03% −2</td></tr>
	<tr><td>入场质量</td><td>基础 8 / 上限 14</td><td>距离 EMA20≤1.2 ATR +4</td><td>距离≥2.2 ATR −5；squeeze/range/mixed/high_volatility −3；观望 −4；压力反向 −4</td></tr>
	<tr><td>风险控制</td><td>基础 10 / 上限 14</td><td>没有额外奖励，重点检查风险是否可控</td><td>费率过热 −3；拥挤 −2；高波动 −3；背离 −2；数据不足 −2；压力反向 −2</td></tr>
	</tbody></table>
	<p>策略模式会对趋势、动量、量价、合约、盘口、风险和高周期影响乘不同权重；风险偏好还会乘风险层系数：保守 1.15、标准 1.0、激进 0.9。每层最终都先四舍五入再限制在 0 与该层上限之间，因此负分不会跨层抵扣。</p>

	<h4>三个容易混淆的分数</h4>
	<table class="help-table"><thead><tr><th>字段</th><th>计算</th><th>用途</th></tr></thead><tbody>
	<tr><td><code>raw_total_score</code></td><td>八层分之和，0–100</td><td>观察强度；≥72 是 L2 候选之一</td></tr>
	<tr><td><code>final_trade_score</code></td><td>最终仍有做多/做空方向时取交易分；被降为观望时为 0</td><td>交易动作等级、跟踪登记和本地视图</td></tr>
	<tr><td><code>confidence</code>（score 内）</td><td>等于 raw_total_score</td><td>本地兼容字段；最终权威 confidence 在 merge 后可能来自 AI</td></tr>
	</tbody></table>""",
            """<table class="help-table"><thead><tr><th>字段</th><th>怎么读</th></tr></thead><tbody>
	<tr><td><code>layer_scores</code></td><td>八层最终分，可直接定位是趋势、量价、资金、盘口、入场还是风险拖后腿</td></tr>
	<tr><td><code>raw_direction</code></td><td>未经过 guard 和入场质量降级的结构倾向</td></tr>
	<tr><td><code>final_direction / direction</code></td><td>本地评分阶段的可执行方向；仍不等于对外 final_decision</td></tr>
	<tr><td><code>direction_guard</code></td><td>非空时说明为什么方向被硬拦截</td></tr>
	<tr><td><code>entry_plan</code></td><td>入场质量、等待条件、失效条件和 ATR/VWAP 参考</td></tr>
	<tr><td><code>strategy_views</code></td><td>同一行情在 scalp、short、swing 三种持仓周期下的独立结论</td></tr>
	<tr><td><code>trends</code></td><td>旧版 5 根首尾比较，仅展示兼容；策略主逻辑读取 trend_profiles</td></tr>
	</tbody></table>
	<div class="help-note"><strong>读日志口诀：</strong>先看 raw_direction 知道规则原本想往哪边，再看 direction_guard 和 entry_plan 知道为什么被拦，最后看当前 strategy_view 是否覆盖。本地 score 有方向，也不代表未调用 AI 时会对外给方向。</div>""",
            """<code>score</code> 主要包含：八层分、raw_total_score、final_trade_score、raw_direction、final_direction、entry/stop_loss/take_profit、entry_plan、strategy_views、structure_forecast、market_regime、sentiment_meta、direction_guard、风险和动作等级。""",
            "AI 触发读取分数与演变；local_screening 保留本地偏向；入场和跟踪读取交易分；post_audit 读取 guard、策略视图和演变方向；Web/压测用 layer_scores 解释结果。",
        ),
        _mod(
            "C2 结构演变 structure_forecast",
            "独立寻找“价格或 5m 已经先动，但 15m 正式结构尚未确认”的过渡态。它用于提前观察拐点和压缩释放，<strong>不会修改 final_decision，也不会替代 trade 信号</strong>；仅在 short/scalp 模式且演变开关开启时运行。",
            """<h4>候选场景与基础概率</h4>
	<table class="help-table"><thead><tr><th>场景</th><th>基础概率</th><th>核心条件</th></tr></thead><tbody>
	<tr><td>mixed_to_up/down</td><td>56</td><td>5m 已转向、15m 仍 mixed/反向、1H 不强烈反向、压力不冲突</td></tr>
	<tr><td>developing_momentum_up/down</td><td>61</td><td>final_direction 仍观望，但短线方向函数已识别 developing_momentum</td></tr>
	<tr><td>profile_lag_up/down</td><td>59</td><td>短窗压力与价格先行，15m profile 尚未跟上</td></tr>
	<tr><td>squeeze_release_up/down</td><td>54</td><td>squeeze/range/mixed，MACD 柱方向允许，等待释放</td></tr>
	<tr><td>structure_near_up/down</td><td>57</td><td>5m 已有方向、15m 正在酝酿，接近 5m+15m 共振</td></tr>
	</tbody></table>
	<p>预测周期按策略隔离：超短使用 3m→5m（15m背景），短线使用 5m→15m（1H背景），中线使用 15m→1H（4H背景），长线使用 4H→1D（1W背景）。各场景按本策略压力窗口、目标周期 MACD/ADX、放量、结构突破和背景周期追加分数，原始概率封顶 88。</p>
	<p><strong>active：</strong>原始概率至少达到 <code>max(45, forecast_push_score−8)</code> 才进入活跃状态。最低 horizon 分别为超短10分钟、短线15分钟、中线240分钟、长线2880分钟。</p>

	<h4>在线校准</h4>
	<p>v2 按 <code>策略 + horizon + 币种 + scenario + direction + regime</code> 建桶，旧版宽松口径的桶不会混入新版概率。样本不足时主要相信规则分；样本达到 <code>calibration_min_samples</code> 后，以 Beta 平滑后的历史命中率按配置权重混合：</p>
	<p><code>校准概率 = 规则概率 × (1−w) + 历史命中率 × 100 × w</code></p>
	<ul>
	<li>历史命中率≥62%：推送门槛最多下调 4；≥52%：下调 1。</li>
	<li>命中率&lt;42%：门槛上调 8；低于禁用线时可自动禁用场景。</li>
	<li>最终 active 还要求场景未禁用，且校准概率≥<code>max(45, effective_threshold−8)</code>。</li>
	</ul>
	<p>在整个 horizon 内持续记录最大顺向/逆向波动。只有目标结构真正确认到 up/down，或窗口内价格沿预测方向达到策略级动态 ATR 阈值，才记为命中；mixed→mixed 不再算命中。结构仅改善会单独记录为 partial_structure_hit。校准同时记录 Brier Score，用于识别概率虚高。</p>""",
            """<table class="help-table"><thead><tr><th>字段</th><th>含义</th></tr></thead><tbody>
	<tr><td><code>scenario / phase</code></td><td>演变类型与 transition/developing/release 阶段</td></tr>
	<tr><td><code>from_state / to_state</code></td><td>预计从什么结构演变到什么结构</td></tr>
	<tr><td><code>raw_probability</code></td><td>规则候选原始分</td></tr>
	<tr><td><code>calibrated_probability</code></td><td>混入历史命中率后的概率，也是最终 probability</td></tr>
	<tr><td><code>invalidation</code></td><td>什么变化出现后该演变判断失效</td></tr>
	<tr><td><code>effective_push_threshold</code></td><td>本场景经校准后的实际推送门槛</td></tr>
	<tr><td><code>scenario_enabled / active</code></td><td>历史表现是否允许继续使用，以及本轮是否达到活跃条件</td></tr>
	</tbody></table>""",
            """<code>score.structure_forecast</code> 保存方向、场景、证据、概率、校准桶、阈值和失效条件；跟踪结果写入 forecast_performance，并更新 calibration_state。""",
            "作为 forecast 独立推送候选；AI trade 可要求与其同向；已被 trade/spike 覆盖、压力反向、scalp 反向或结构已确认时会阻止 forecast。",
        ),
        """
	</section>

	<section class="card help-card"><h2>§D 方向决策 raw_direction · guard · 入场</h2>""",
        _mod(
            "D 方向阶梯与拦截",
            "先按策略周期选出最可能的本地方向，再通过短窗压力、周期票、风险偏好和入场质量逐级拦截。这里区分<strong>“市场偏向哪边”</strong>与<strong>“现在是否适合执行”</strong>：raw_direction 可以偏多，但 final_direction 仍可能是观望。",
            """<h4>四种策略的方向逻辑</h4>
	<table class="help-table"><thead><tr><th>模式</th><th>主要窗口</th><th>方向形成</th><th>典型过滤</th></tr></thead><tbody>
	<tr><td>scalp</td><td>1m/3m/5m，5–10m 涨跌</td><td>5m 或 10m 达阈值；短周期至少 2 票；或从近低点反弹/近高点回落达到阈值</td><td>15m 强烈反向时，除非 5m 脉冲达到普通阈值的 1.35 倍，否则观望</td></tr>
	<tr><td>short</td><td>5m/15m，10–20m 动量</td><td>优先使用 market bias，其次 5m+15m 共振，再看 developing_momentum、developing、20m momentum；激进模式可使用 pressure</td><td>1H 强反向、短窗压力冲突、trade 票不足</td></tr>
	<tr><td>swing</td><td>1H/4H，15m 确认，30–60m 动量</td><td>1H+4H 同向直接 aligned；或 1H/15m 先行、4H 不反向；再允许足够强的中线 momentum</td><td>4H 反向、15m 未确认、短窗压力反向</td></tr>
	<tr><td>long</td><td>1D/1W，4H 确认</td><td>1D+1W 同向，或 1D 主趋势已形成且 4H 确认、1W 不反向</td><td>1W 反向、4H 未确认、日线结构失效</td></tr>
	</tbody></table>
	<p>风险偏好会缩放动量阈值：保守 ×1.15、标准 ×1.0、激进 ×0.9；scalp 激进使用 ×0.82。scalp 默认阈值约 5m=0.22%、10m=0.35%；short 阈值由 15m ATR% 动态生成；swing 使用 30m/60m 与 ATR 联动。</p>

	<h4>direction_guard 如何拦截</h4>
	<ul>
	<li><strong>scalp：</strong>标准/保守下，做多遇 pressure=down 或做空遇 pressure=up 直接拦；激进不拦。</li>
	<li><strong>short：</strong>若 20m 已形成足够强的同向延伸可放行；否则压力反向且 bias/交易票不支持时拦。压力中性时，标准/激进至少要 1 张 trade 票，保守要 2 张。</li>
	<li><strong>swing：</strong>压力反向且大结构 bias 不支持时拦；强 sentiment 在非保守模式可提供豁免。</li>
	<li>guard 返回具体原因字符串，例如 <code>recent_price_pressure_down_blocks_long</code>，非空即把本地 final_direction 降为观望。</li>
	</ul>

	<h4>入场区、止损和止盈</h4>
	<p>系统使用 5m/15m ATR、15m 结构高低点、5m EMA20 和最近 60 根 1m K 线近似 VWAP。VWAP 使用典型价格 × 成交量计算，不是交易所逐笔精确 VWAP。</p>
	<table class="help-table"><thead><tr><th>项目</th><th>做多</th><th>做空</th></tr></thead><tbody>
	<tr><td>锚点</td><td>VWAP、5m EMA 与 price−ATR 中较高者</td><td>VWAP、5m EMA 与 price+ATR 中较低者</td></tr>
	<tr><td>止损</td><td>15m 结构低点下方，并至少留出 5m ATR 缓冲</td><td>15m 结构高点上方，并至少留出 5m ATR 缓冲</td></tr>
	<tr><td>止盈</td><td>按风险距离的 1.2R / 2.0R</td><td>按风险距离的 1.2R / 2.0R</td></tr>
	<tr><td>等待项</td><td>盘口买盘、15m 上破或回踩不破</td><td>盘口卖压、15m 下破或反抽不过</td></tr>
	</tbody></table>
	<p><code>quality=breakout_valid</code> 只在趋势市且等待项为空时出现；有方向但条件不完整为 <code>wait_confirmation</code>；方向不清晰为 <code>no_trade</code>。</p>

	<h4>入场质量后的二次降级</h4>
	<p>guard 命中一定降级。否则根据模式、quality 与 <code>direction_score</code> 判断是否保留方向。当前调用传给 <code>_should_downgrade_direction</code> 的实际值是 direction_score，虽然函数形参仍保留旧名 raw_total_score。标准风险偏好下，wait_confirmation 的基础门槛为 scalp 55、short 60、swing 64、long 68；保守 +6，激进 −5。强情绪领先时可在达到对应门槛后保留方向；价格领先且短窗压力同向时可取消质量降级。</p>""",
            """<table class="help-table"><thead><tr><th>字段</th><th>含义</th></tr></thead><tbody>
	<tr><td><code>raw_direction</code></td><td>当前策略路径最倾向的做多/做空/观望</td></tr>
	<tr><td><code>direction_guard</code></td><td>硬拦截原因；非空时当前策略不可直接执行</td></tr>
	<tr><td><code>final_direction</code></td><td>guard、质量降级和选中策略视图后的本地方向</td></tr>
	<tr><td><code>entry_plan.quality</code></td><td>breakout_valid / wait_confirmation / no_trade</td></tr>
	<tr><td><code>wait_for</code></td><td>从观望升级为可执行还缺哪些条件</td></tr>
	<tr><td><code>invalidation</code></td><td>结构判断失效的明确价格行为</td></tr>
	</tbody></table>""",
            """写入 score 的 raw_direction、final_direction、direction_guard、entry、stop_loss、take_profit、entry_plan，以及三种 strategy_views。""",
            "本地方向供评分和演变使用；AI启用但本轮未调AI或调用失败时，final_decision 保留 score.final_direction，并以 final_trade_score 达到方向推送阈值 +5 作为本地 trade 的首道资格。AI整体关闭时，Web走势图与压测直接使用本地score，不改写本地分析对象。所有 trade 在 post_audit 仍会再次执行方向冲突检查。",
        ),
        """
	</section>

	<section class="card help-card"><h2>§E AI 触发 evaluate_ai_trigger</h2>""",
        _mod(
            "E 触发等级 L0–L3",
            "把本地异常事件分为 L0–L3，决定是否值得花成本调用模型。等级描述的是<strong>分析优先级</strong>，不是交易强度；L2 也可能最终观望，L3 也可能被冲突复核拦截。",
            """<table class="help-table"><thead><tr><th>等级</th><th>形成条件</th><th>调用行为</th></tr></thead><tbody>
	<tr><td>L0</td><td>signals 为空</td><td>不调用 AI，fingerprint 为空</td></tr>
	<tr><td>L1</td><td>至少一个信号，但未达到升级条件</td><td>本地筛查，不调用 AI</td></tr>
	<tr><td>L2</td><td>多信号、合格交易信号、raw≥72、多观察信号、情绪领先/冲突、活跃演变等</td><td>还需通过 L2 资格和去重，才置 should_call_ai=true</td></tr>
	<tr><td>L3</td><td>scalp 急变达到策略有效分；中线另需强证据、方向/15m 对齐及连续两轮；或 funding_hot 达基础阈值 1.25 倍</td><td>ai_enabled 时进入高优先调用；未达到中线急变资格时按普通 L1/L2 处理</td></tr>
	</tbody></table>

	<h4>L2 升格原因</h4>
	<ul>
	<li><code>multi_signal</code>：signals 数量≥2。</li>
	<li><code>sentiment_leading</code>：情绪强度≥3，且与 raw_direction 同向。</li>
	<li><code>sentiment_structure_conflict</code>：本地原有方向，但被降为观望，情绪仍有强方向。</li>
	<li><code>sentiment_signals</code>：OI/费率快变/拥挤类信号配合情绪强度≥2。</li>
	<li><code>trade_signal</code>：交易类信号满足结构要求。</li>
	<li><code>raw_score_high</code>：raw_total_score≥72。</li>
	<li><code>multi_watch</code>：至少两类 WATCH 信号。</li>
	</ul>

	<h4>为什么“显示 L2”仍可能不调 AI</h4>
	<p><code>_l2_qualifies_ai_call</code> 会再次过滤：</p>
	<ul>
	<li>超短/短线中，multi_signal、multi_watch、情绪领先/冲突、raw 高分可直接合格；中线/长线还须通过强证据、方向分和周期对齐复核；</li>
	<li>活跃 structure_forecast 合格；</li>
	<li>trend_up/down/squeeze 中出现结构、放量、OI 或盘口信号合格；</li>
	<li>只有 MACD 一条信号明确不合格；只有一条 trade_signal 也不合格；</li>
	<li>sentiment_signals 至少还要两条信号。</li>
	</ul>
	<p>合格后生成指纹 <code>排序后的信号类型:score.direction:raw分数5分桶</code>。超短/短线仍允许指纹变化形成新调用；中线普通 L2 对同币种执行至少 300 秒硬间隔，长线至少 600 秒，间隔内即使信号组合或分数桶变化也不重复调用。L3 使用基础间隔并保留高优先事件能力。</p>
	<div class="help-note"><strong>中线/长线 L2 成熟条件：</strong>至少包含 volume_spike、structure_break、oi_change 之一；方向必须有效，方向分达到风险偏好对应门槛，并且中线的15m/1H、长线的4H/1D不能反向且至少一层同向。单独 MACD、盘口变化、低分 forecast 或普通多信号只记录，不调用 AI。</div>
	<p>未通过成熟复核的原始 L2 候选会降回 <code>level=L1</code>，同时保留 <code>candidate_level=L2</code> 和 <code>skip_reason=l2_not_qualified</code> 供日志解释，避免界面继续把普通噪声显示成正式 AI 触发。</p>
	<p>当前方向分基础门槛：中线标准53、长线标准57；保守 +4，激进 −4。两个强证据同时出现时可再放宽4分，但周期对齐仍为必需条件。该分数只决定是否值得调用 AI，不等于交易推送分。</p>
	<h4>中线补充触发通道</h4>
	<ul>
	<li><code>sustained_displacement</code>：30–60分钟同向位移达到约0.8–1.0倍1H ATR，方向分接近成熟门槛且15m/1H不反向；持续行情每5分钟最多复核一次。</li>
	<li><code>high_probability_forecast</code>：结构演变场景有效，概率至少高于有效 forecast 门槛5分，并与本地方向及周期对齐。</li>
	<li><code>direction_reversal</code>：本地方向从做多切换为做空或反向，方向分和周期对齐有效；相对于上次AI分析确属新方向时允许绕过一次普通5分钟间隔。</li>
	</ul>
	<p>三条通道只增加 AI 复核机会，不直接产生交易推送；最终仍须经过 merge、post_audit 和微信门禁。</p>
	<div class="help-note"><strong>注意：</strong>5 分桶可以减少 raw 分数小幅抖动造成的重复 AI 请求；信号类型、方向或分数跨桶仍会形成新指纹。L3 不检查普通 L2 指纹间隔，但仍要求 ai_enabled。</div>""",
            """<code>level</code> 是等级；<code>reasons</code> 是升格原因；<code>fingerprint</code> 用于去重；<code>should_call_ai</code> 才表示本轮实际应调用；<code>ai_invoked</code> 在真正调用后由主流程改为 true。""",
            """写入 <code>local_trigger</code>，同时带 local_hint：本地方向、分数、风险、价位、市场状态、情绪和信号类型。""",
            "主流程仅在 should_call_ai=true 时进入 AI；merge 用 ai_invoked 区分 local_screening 与 local_fallback；推送和日志保留触发等级与原因。",
        ),
        """
	</section>

	<section class="card help-card"><h2>§F AI 分析 analyze_with_ai</h2>""",
        _mod(
            "F 模型调用与熔断",
            "在 trigger 明确要求调用后，把裁剪后的<strong>原始市场快照</strong>、触发上下文（含 signal_evidence）、策略配置门槛交给模型，<strong>不传</strong>本地评分/方向/structure_forecast；要求返回结构化 JSON 与未来 horizon 的 <code>forward_view</code>。任何安装、配置、网络、熔断或格式失败都不会停止监控，而是返回本地 fallback。",
            """<h4>调用前检查顺序</h4>
	<ol class="help-list">
	<li><strong>dry-run：</strong>只构造 payload，不发送请求；返回 provider=dry-run。</li>
	<li><strong>回放缓存：</strong>replay 模式按 <code>inst_id + signal fingerprint</code> 查缓存，有有效结果直接复用。</li>
	<li><strong>包检查：</strong>无法导入 openai 时标记 <code>package_missing</code>。</li>
	<li><strong>密钥检查：</strong>读取 AI_API_KEY 或 OPENAI_API_KEY；缺失时标记 <code>config_missing</code>。</li>
	<li><strong>熔断检查：</strong>open 状态先尝试轻量 ping；未恢复则返回 circuit_open fallback。</li>
	<li><strong>正式调用：</strong>temperature=0.2，客户端自身重试关闭，由系统统一重试。</li>
	</ol>

	<h4>请求重试与熔断状态机</h4>
	<table class="help-table"><thead><tr><th>状态</th><th>进入条件</th><th>行为</th></tr></thead><tbody>
	<tr><td>closed</td><td>失败次数&lt;阈值，默认 3</td><td>正常调用</td></tr>
	<tr><td>open</td><td>失败达到阈值</td><td>默认 120 秒内跳过 chat，走本地 fallback</td></tr>
	<tr><td>half_open</td><td>冷却结束但尚未探活成功</td><td>按默认 60 秒探活间隔发送最多 5 token 的 ping</td></tr>
	</tbody></table>
	<ul>
	<li>可重试错误按 <code>retry_backoff × attempt</code> 退避；限流错误至少使用专用 rate-limit backoff。</li>
	<li>连接错误重试前重建客户端；401/403 或不可重试错误直接把失败计数拉到阈值。</li>
	<li>任一次正式请求或探活成功都会清空失败计数、关闭熔断并清除异常状态。</li>
	</ul>

	<h4>请求 payload 结构（自主分析）</h4>
	<table class="help-table"><thead><tr><th>区块</th><th>内容</th><th>说明</th></tr></thead><tbody>
	<tr><td><code>market_data</code></td><td>K线、<code>bar_profiles</code>、衍生品、盘口、<code>market_context</code>、数据质量</td><td><strong>事实来源</strong>；K 线 <code>newest_first</code>；bar_profiles 为中性技术指标</td></tr>
	<tr><td><code>trigger_context</code></td><td>触发等级、原因、信号列表、<code>signal_evidence</code>（current vs threshold，无 valid_by_rule）</td><td>说明为何调用；信号用 <code>breakout: up/down</code> 事实描述</td></tr>
	<tr><td><code>analysis_config</code></td><td>策略模式、推送阈值、检测阈值</td><td>约束输出语义，非预计算结论</td></tr>
	</tbody></table>
	<p>不再向模型传递 <code>local_screening</code>、<code>local_reference</code>、本地分、<code>structure_forecast</code> 等结论；<code>build_local_screening</code> 仍写入 <code>final_decision</code> 供日志与 Web 展示。</p>

	<h4>成功响应处理</h4>
	<div class="flow-chain"><span>读取 message.content</span><span>提取 JSON 对象</span><span>normalize</span><span>字段校验</span><span>生成 forward_view</span><span>记录 token</span></div>
	<p>输出即使包含文本也必须能提取出合法 JSON；normalize 会补齐方向、置信度、风险、价位、trend 和 forward 默认 horizon。校验失败时保留原文与 validation_errors，但 <code>valid_json=false</code>，merge 不采用其方向。</p>
	<p>回放模式仅缓存 valid_json 的成功结果；token 统计记录 prompt、completion、total，接口不返回 usage 时不会伪造。</p>

	<h4>AI 异常微信告警</h4>
	<p>异常持续默认 300 秒后发送运维告警，同类告警默认冷却 3600 秒。要求已启用 AI、非 dry-run、非 replay，并配置 WECHAT_SEND_KEY；<strong>不依赖交易信号 push_enabled</strong>。正文包含异常类型、持续时间、熔断状态、失败次数、模型、接口和原始失败原因。</p>""",
            """<table class="help-table"><thead><tr><th>字段</th><th>含义</th></tr></thead><tbody>
	<tr><td><code>provider/model</code></td><td>调用来源和模型；fallback 时 provider=local</td></tr>
	<tr><td><code>ai_status</code></td><td>closed/open/half_open 或具体 fallback 状态</td></tr>
	<tr><td><code>content</code></td><td>模型原始文本或 fallback 说明</td></tr>
	<tr><td><code>parsed</code></td><td>标准化后的模型 JSON</td></tr>
	<tr><td><code>valid_json</code></td><td>是否通过结构校验，决定 merge 能否采用</td></tr>
	<tr><td><code>fallback/error</code></td><td>本地分析及失败原因</td></tr>
	<tr><td><code>usage</code></td><td>API 返回的 token 使用量</td></tr>
	</tbody></table>
	<p><code>forward_view</code> 是未来 horizon 的方向、概率、入场计划和失效条件；它是 AI 前瞻压测的主对象，不等同于对历史行情的文字总结。</p>""",
            """<code>analysis</code> 原样写入 JSONL；有效结果进入 AI final_decision，无效或失败结果触发 local_fallback。""",
            "merge 判断权威来源；post_audit 对 AI trade 再复核；decision 校准按 AI/方向/regime 建桶；微信展示摘要；压测独立统计 ai_forward 命中率。",
        ),
        """
	</section>

	<section class="card help-card"><h2>§G 合并 merge_final_decision</h2>""",
        _mod(
            "G 权威结论出口",
            "把 AI 结果与本地筛查合并成每轮唯一的对外结论。推送读取 final_decision；AI整体关闭时，Web图表、模拟账户和压测改读原始本地score，以独立验证本地分析。",
            """<h4>来源选择只有一条判断</h4>
	<table class="help-table"><thead><tr><th>条件</th><th>decision_source</th><th>对外方向</th></tr></thead><tbody>
	<tr><td>analysis.valid_json=true 且 parsed 为对象</td><td><code>ai</code></td><td>优先 forward_view.direction，其次 parsed.direction</td></tr>
	<tr><td>AI启用，但本轮没有真正调用 AI</td><td><code>local_screening</code></td><td>方向、交易分和入场计划来自本地 score；达到方向阈值 +5 才推荐 trade</td></tr>
	<tr><td>AI整体关闭</td><td><code>local_screening</code></td><td>方向、走势图、准确率、模拟曲线和最终决策统一使用本地 score</td></tr>
	<tr><td>本轮调用了 AI，但失败或 JSON 无效</td><td><code>local_fallback</code></td><td>忽略失败 AI 内容，保留本地方向；达到方向阈值 +5 才推荐 trade</td></tr>
	</tbody></table>
	<div class="help-note"><strong>关键设计：</strong>本地 <code>score.final_direction</code> 是未调用 AI 或 AI 失败时的兜底方向，但不是无条件推送。只有 <code>final_trade_score ≥ trade_push_score(direction) + 5</code>，且没有 direction_guard、质量降级或 no_trade 入场计划，才会生成本地 trade 推荐；否则为 watch/none。AI 成功时仍完全采用有效 AI 结果。</div>

	<h4>AI confidence 如何限制</h4>
	<p>先读取模型 confidence，并与 forward probability 取较强参考；若缺失则回退本地分。最终还会受本地证据上限限制：大致不超过 <code>max(raw_total+15, short_view+8, 52)</code>；有 forward probability 时允许上限提高到 probability+6，最高仍为 100。这样模型不能在本地证据很弱时随意报极高置信度。</p>

	<h4>push_recommendation 推导</h4>
	<ol class="help-list">
	<li>模型显式给出 none/watch/trade/spike 时优先采用；特殊情况下 L3 与反向 scalp 会把 trade 改为 spike。</li>
	<li>没有显式值时，L3 且 scalp 急变、方向有效 → spike。</li>
	<li>方向为做多/做空且 confidence 达对应 trade 门槛 → trade。</li>
	<li>方向观望、达到 watch 分且存在 WATCH 信号 → watch。</li>
	<li>其余 → none。</li>
	</ol>
	<p>若 AI 数据质量被标记不可信，trade 会降为 watch 或 none；高风险且置信度偏低的 trade 也会降为 watch。</p>""",
            """<table class="help-table"><thead><tr><th>字段</th><th>权威含义</th></tr></thead><tbody>
	<tr><td><code>direction</code></td><td>本轮对外方向；Web 合并轨和 paper 默认读取它或 AI forward</td></tr>
	<tr><td><code>local_bias</code></td><td>本地规则倾向，仅供参考</td></tr>
	<tr><td><code>confidence</code></td><td>合并后的置信度，仍要经过 post_audit 和 push_gate</td></tr>
	<tr><td><code>push_recommendation</code></td><td>候选类型，不代表一定发送</td></tr>
	<tr><td><code>forward_view</code></td><td>AI 前瞻详情；仅 AI 来源存在可靠意义</td></tr>
	<tr><td><code>local_screening</code></td><td>本地对已发生行情的摘要与信号过滤</td></tr>
	<tr><td><code>rule_audit</code></td><td>AI 输出的数据质量/规则审计</td></tr>
	</tbody></table>""",
            """输出 final_decision：direction、local_bias、confidence、push_recommendation、entry/stop_loss/take_profit、risk、summary、reasons、decision_source、trigger_level、forward_view、local_screening、market_regime 等。""",
            "随后必须经过 post_audit；跟踪和模拟读取审计后的结果；push_gate 只接受满足类型开关、方向和分数门槛的 recommendation。",
        ),
        """
	</section>

	<section class="card help-card"><h2>§H 推送复核 post_audit · push_gate</h2>""",
        _mod(
            "H 冲突拦截与门槛",
            "merge 只产生候选结论；H 层负责两次把关：<strong>post_audit 检查逻辑冲突</strong>，<strong>push_gate 检查开关和硬门槛</strong>。因此“AI 建议 trade”与“实际发送 trade”之间还隔着完整安全层。",
            """<h4>post_audit 顺序</h4>
	<ol class="help-list">
	<li>若开启 <code>l3_local_spike_push</code>，且 L3 原因是 scalp_spike，本地 scalp 方向可直接覆盖为 spike，并提高 confidence。</li>
	<li>L3 中 AI trade 与 scalp 同向时转为 spike，避免把急变误包装成普通结构单。</li>
	<li>开启 ai_conflict_guard 后，trade/spike 与活跃 scalp 反向 → 降级或阻断。</li>
	<li>做空 trade 遇 pressure=up、做多 trade 遇 pressure=down → 降级。</li>
	<li>重新执行 direction_guard；仍不合格则降级。</li>
	<li>trade confidence 只比门槛高 0–3 分时视为“压线”：震荡/混合且 bias 中性时降为 watch/none。</li>
	<li>若开启 forward/forecast 对齐，AI trade 与活跃 structure_forecast 反向时降级。</li>
	<li>最后应用 AI 历史校准：样本足够且命中率低于禁用线时降级；命中率≥60% 且仅差门槛 2 分时可补到门槛。</li>
	</ol>
	<p>trade 或 spike 降级时，如果 confidence 已达 watch_score 且 watch 开关开启，通常变为 watch；否则为 none。</p>

	<h4>push_gate 硬条件</h4>
	<table class="help-table"><thead><tr><th>类型</th><th>必须满足</th></tr></thead><tbody>
	<tr><td>spike</td><td>spike 开关开启；超短/短线 confidence≥spike_push_score；中线有效门槛为 spike_push_score+10，并要求 volume/structure/OI/显著短窗位移之一，以及方向或15m确认一致。活跃 scalp 分可抬高有效 confidence</td></tr>
	<tr><td>watch</td><td>watch 开关开启；confidence≥watch_push_score；非 AI 且方向观望时必须存在 WATCH 信号</td></tr>
	<tr><td>trade</td><td>trade 开关开启；方向必须做多/做空；confidence 达做多 push_score 或做空 short_push_score；forward/forecast 不冲突</td></tr>
	</tbody></table>

	<h4>forecast 独立门禁</h4>
	<p>forecast 必须 active、场景未被校准禁用、校准概率达到 effective threshold，并且：</p>
	<ul>
	<li>方向有效，且不与活跃 scalp 或短窗压力冲突；</li>
	<li>同方向 trade/spike 尚未覆盖；</li>
	<li>结构没有已经确认到足以无需“演变提醒”；</li>
	<li>AI forward 与 forecast 对齐（配置开启且 AI 来源时）。</li>
	</ul>

	<h4>冷却</h4>
	<p>推送键由 <code>kind + inst_id + direction</code> 组成。trade、spike、watch 默认各 900 秒，forecast 默认 1800 秒；中线 spike 实际最短冷却为 1800 秒，长线为 3600 秒；反向 trade 另有默认 300 秒冷却。同币种微信还有全局最小间隔，见 §J。</p>""",
            """<code>post_audit.action</code>：kept、blocked、downgraded、l3_local_spike；<code>reasons</code> 保存每条具体原因。watch 且观望会附加 <code>watch_no_direction</code>，表示这是观察提醒而非方向单。""",
            """审计结果写回 final_decision.post_audit；回放额外生成 push_analysis，逐轨记录 skipped、gate_blocked、blocked 或 would_push 及原因。""",
            "微信分发只接收通过 gate 的轨道；日志和控制台可以用 reason 精确回答“为什么没有推”。",
        ),
        """
	</section>

	<section class="card help-card"><h2>§J 微信推送 dispatch_wechat_push_if_needed</h2>""",
        _mod(
            "J 对外通知",
            "把通过 post_audit 与 push_gate 的 confirmed 轨（trade/spike/watch）和 forecast 轨放在一起竞争，每轮每币种最多选择一条，通过 Server酱发送结构化微信。微信层还有比内部 gate 更严格的防打扰门槛。",
            """<h4>两条候选轨</h4>
	<ul>
	<li><strong>confirmed：</strong>读取 final_decision.push_recommendation，经 push_gate、反向 trade 冷却和类型冷却后成为 would_push。</li>
	<li><strong>forecast：</strong>读取 structure_forecast，经概率、冲突、覆盖和 forecast 冷却后成为 would_push。</li>
	</ul>

	<h4>微信专用附加门槛</h4>
	<table class="help-table"><thead><tr><th>类型</th><th>内部 gate 通过后还需满足</th></tr></thead><tbody>
	<tr><td>watch</td><td>必须是 AI 来源且本轮真的调用了 AI；AI 明确推荐 watch；confidence≥watch_score+5</td></tr>
	<tr><td>spike</td><td>AI 不能绕过策略资格：中线须达到 spike_score+10、强证据、方向/15m 对齐和连续确认；本地 L3 spike 还必须开启 l3_local_spike_push</td></tr>
	<tr><td>trade</td><td>方向有效；L2/L3 可直接通过附加条件，否则 confidence≥对应 trade 门槛+3</td></tr>
	<tr><td>forecast</td><td>概率≥有效门槛+7；同时要求 L2/L3，若不是 L2/L3 则概率至少达到门槛+12</td></tr>
	</tbody></table>

	<h4>竞争、优先级与冷却</h4>
	<ol class="help-list">
	<li>若 confirmed 中存在 trade 或 spike，forecast 直接标记 <code>wechat_superseded_by_confirmed</code>。</li>
	<li>剩余候选按 <code>trade &gt; spike &gt; forecast</code> 排序。watch 属于 confirmed，但不在显式优先数组中，因此通常排在这些类型之后。</li>
	<li>同币种距离上次任何微信不足 600 秒，所有候选标记 <code>wechat_inst_cooldown</code>。</li>
	<li>再检查反向 trade 冷却和 kind/币种/方向冷却，选择第一条可发送轨。</li>
	<li>其他 would_push 候选标记 <code>wechat_superseded</code> 或 cooldown。</li>
	</ol>

	<h4>实际发送行为</h4>
	<ul>
	<li><code>push_enabled=false</code>：不请求 Server酱，但记录 dry-run 推送事件并写入冷却，防止日志每轮重复。</li>
	<li>未配置 WECHAT_SEND_KEY：记录 skipped(no wechat key)，同样写入冷却。</li>
	<li>配置完整：POST 到 Server酱接口，成功后记录类型冷却、币种微信时间；trade 另记录最后方向供反向冷却。</li>
	<li>回放模式可仅生成 <code>push_analysis</code>，明确显示理论上是否会推，不必真的发送。</li>
	</ul>""",
            """微信内容是行情快照、方向、置信度、价位、失效条件、信号和 AI/本地说明的结构化提醒。它代表“系统认为值得通知”，不是交易所订单，也不保证成交。""",
            """真实发送走 Server酱 HTTP；回放/关闭推送时写控制台与 push_analysis。每币种每轮最多一条。""",
            "用户通知、防刷屏和推送复盘；推送是否发送不影响 signal/forecast/decision 的事后样本登记。",
        ),
        """
	</section>

	<section class="card help-card"><h2>§I 跟踪 · 模拟 · §K 日志</h2>""",
        _mod(
            "I 事后结算与 paper",
            "建立三个独立的事后验证闭环，并维护一个简化模拟账户：①交易计划是否触达并盈利；②结构演变是否命中；③最终决策在固定窗口是否命中。历史结果可校准未来概率和 AI 推送，但不会篡改已经发生的当轮结论。",
            """<h4>1. signal_tracking：验证交易计划</h4>
	<p>仅当 final_decision 有做多/做空方向、推荐 trade/spike，且 confidence≥<code>max(70, 对应trade门槛−10)</code> 时登记。相同币种+方向+策略模板+信号组合 60 秒内不重复。</p>
	<ol class="help-list">
	<li>同时建立 5m、15m、1H 三个 horizon 样本。</li>
	<li>先处于 <code>pending_entry</code>，等待建议入场区被最新 1m high/low 或当前价触达。</li>
	<li>15 分钟未触达则 <code>expired_unfilled</code>，收益记 0，不算成交命中。</li>
	<li>触达后进入 <code>active_review</code>；做多按区间上沿、做空按区间下沿保守估算成交。</li>
	<li>每轮更新最大顺向波动 MFE 和最大逆向波动 MAE；到期时按方向收益&gt;0 判 hit。</li>
	</ol>
	<p>聚合维度为币种+方向+strategy_template+horizon，统计成交率、命中率、平均收益、平均 MFE/MAE、最好/最差收益。</p>

	<h4>2. forecast_tracking：验证演变预测</h4>
	<p>active forecast 按策略+horizon+scenario+direction 去重，完整 horizon 内不重复登记，避免重叠样本放大命中率。v2 待结算样本随 calibration_state 持久化，监控重启后继续跟踪中线/长线窗口。窗口内持续更新后：</p>
	<ul>
	<li>策略目标周期真正确认到预测方向，记 structure_hit；mixed/flat/range 原地不动不算命中；</li>
	<li>只发生结构改善但未确认，记 partial_structure_hit，仅供诊断；</li>
	<li>窗口内最大顺向价格变化达到动态 ATR 阈值，记 price_hit；</li>
	<li>任一命中即 overall hit，并更新 forecast 校准桶。</li>
	</ul>

	<h4>3. decision calibration：验证最终方向</h4>
	<p>final_decision 方向有效、recommendation 为 trade/spike/watch 且 confidence≥50 时登记，固定 15 分钟结算；相同来源+类型+方向+regime 300 秒内不重复。命中标准同样是结构或价格任一达标。</p>

	<h4>4. paper_account：方向换仓模拟</h4>
	<ul>
	<li>初始资金 $10,000，1 倍满仓，方向变化时先平旧仓再开新仓，观望时空仓。</li>
	<li>多仓权益按 price/entry 变化；空仓按 entry−price 的百分比变化；开仓和平仓都扣 <code>paper_fee_bps</code>。</li>
	<li><code>paper_follow_ai_only=true</code> 时，只跟随 decision_source=ai 的 forward_view；其他来源一律观望。</li>
	<li>这是按轮询价估算的方向模拟，不包含滑点、资金费率、强平和真实订单深度。</li>
	</ul>""",
            """<table class="help-table"><thead><tr><th>概念</th><th>含义</th></tr></thead><tbody>
	<tr><td>fill_rate</td><td>建议入场区最终被触达的比例</td></tr>
	<tr><td>hit_rate</td><td>已成交样本中，到期方向收益为正的比例</td></tr>
	<tr><td>MFE / MAE</td><td>持有窗口内最大顺向/逆向波动，用于评估止盈止损空间</td></tr>
	<tr><td>structure_hit</td><td>策略目标周期真正确认到预测方向</td></tr>
	<tr><td>partial_structure_hit</td><td>结构向目标方向改善但尚未确认，不计 overall hit</td></tr>
	<tr><td>price_hit</td><td>窗口内最大顺向价格变化达到 ATR 自适应阈值</td></tr>
	<tr><td>Brier Score</td><td>概率预测误差，越低越好；高于阈值时自动压低校准概率</td></tr>
	<tr><td>paper PnL</td><td>持续跟随方向换仓的组合结果，不等于单条推送胜率</td></tr>
	</tbody></table>""",
            """signal_performance.jsonl 保存交易样本；forecast_performance.jsonl 保存演变结算；decision_calibration.jsonl 保存最终决策结算；calibration_state.json 保存聚合桶；paper_account.json 保存模拟账户。""",
            "Web 压测、模拟权益曲线、结构概率校准、AI 低命中桶降级和后续参数优化。",
        ),
        _mod(
            "K 持久化 log_result",
            "每轮、每币种都把从行情输入到最终结论的关键状态写成一行 JSONL，即使没有信号也记录。日志是 Web 图表、准确度统计、问题复盘和诊断导出的权威历史来源。",
            """<h4>写入时机</h4>
	<p>顺序为：采集 → 检测 → 评分 → AI → merge → post_audit → forecast/decision/signal 跟踪 → paper → 推送 → log_result。因此日志中的 final_decision 已完成复核，tracking 和 paper 也是本轮更新后的值。</p>
	<p>实时模式可通过 analysis_log_enabled 关闭；回放模式始终写 replay 日志并立即 flush，方便边跑边看。</p>

	<h4>主要字段组</h4>
	<table class="help-table"><thead><tr><th>字段</th><th>内容</th></tr></thead><tbody>
	<tr><td>time / inst_id / price</td><td>本轮身份和价格</td></tr>
	<tr><td>chart</td><td>最近 21 个 1m 紧凑 K 线点，供快速图表</td></tr>
	<tr><td>原始/衍生市场数据</td><td>OI、成交量、费率、多空比、盘口、trend_profiles、volatility、dynamic_thresholds、market_context</td></tr>
	<tr><td>signals</td><td>本轮命中的异常事件列表</td></tr>
	<tr><td>score</td><td>八层分、本地方向、策略视图、入场计划和结构演变</td></tr>
	<tr><td>local_trigger</td><td>L0–L3、调用原因、指纹和本地提示</td></tr>
	<tr><td>analysis</td><td>AI 原文、解析、校验、错误和 token；未调用时为 null</td></tr>
	<tr><td>final_decision</td><td>审计后的权威结论</td></tr>
	<tr><td>signal_tracking / paper_account</td><td>当轮交易计划样本的打开/关闭结果及模拟账户；forecast/decision 结算写各自专项 JSONL，不完整内嵌在主日志</td></tr>
	<tr><td>calibration_summary</td><td>当前币种 forecast/decision 校准桶摘要</td></tr>
	<tr><td>config_snapshot</td><td>当轮关键配置快照，避免以后用新配置误解释旧日志</td></tr>
	<tr><td>push_analysis</td><td>仅回放：每条推送轨理论状态与阻塞原因</td></tr>
	</tbody></table>

	<h4>轮转与容量</h4>
	<p>默认主分析日志单文件 500MB，总容量 1.5GB；达到条件后轮转并清理最旧分卷。signal performance 等专项日志有各自轮转。日志轮转只影响历史保留，不改变运行中的判断。</p>""",
            """一行不是“一个信号”，而是<strong>一个币种在一个轮询时刻的完整状态机快照</strong>。分析字段为空不代表程序失败，可能只是 L0/L1 没有调用 AI。""",
            """实时写入 okx_signal_analysis.jsonl；回放写 replay_analysis.jsonl。JSON 使用 UTF-8、ensure_ascii=false，每行一个独立对象。""",
            "Web K 线与快照、accuracy API、回放对比、token 统计、诊断 ZIP、配置变更前后归因。",
        ),
        """
	</section>

	<section class="card help-card"><h2>附录：配置 · 压测 · 环境变量</h2>
	<h3>配置如何影响整条链路</h3>
	<table class="help-table"><thead><tr><th>配置</th><th>默认</th><th>调高/开启后的效果</th><th>主要阶段</th></tr></thead><tbody>
	<tr><td>strategy_mode</td><td>short</td><td>切换 scalp/short/swing 的方向窗口、评分权重、持仓时间和 AI K 线裁剪</td><td>§C §D §F</td></tr>
	<tr><td>risk_preference</td><td>standard</td><td>保守提高动量与确认要求；激进降低阈值并放宽部分 guard</td><td>§C §D</td></tr>
	<tr><td>volume_multiplier</td><td>2.0</td><td>越高越不容易触发放量；实际与动态 P85 取较高值</td><td>§A3 §B</td></tr>
	<tr><td>oi_change_pct_15m</td><td>5.0%</td><td>越高越少触发 OI 异动</td><td>§B</td></tr>
	<tr><td>funding_abs / change</td><td>0.0008 / 0.0003</td><td>越高越少触发费率过热/快变，也改变合约评分和情绪积分</td><td>§A3 §B §C</td></tr>
	<tr><td>long_short_extreme</td><td>0.75</td><td>越高越少触发拥挤信号</td><td>§A3 §B §C</td></tr>
	<tr><td>push_score / short_push_score</td><td>75 / 75</td><td>提高后做多/做空 trade 更难通过；也影响跟踪登记底线</td><td>§G §H §I §J</td></tr>
	<tr><td>watch_push_score</td><td>65</td><td>提高后观察提醒和 trade/spike 降级到 watch 更困难</td><td>§G §H §J</td></tr>
	<tr><td>spike_push_score</td><td>62</td><td>超短/短线直接使用；中线自动 +10、长线自动 +15。中线还要求强证据、方向/15m 对齐和连续两轮</td><td>§D §E §H §J</td></tr>
	<tr><td>forecast_push_score</td><td>58</td><td>提高后演变 active 和 forecast 推送都更严格；实际门槛受校准调整</td><td>§C2 §H §J</td></tr>
	<tr><td>signal_*_enabled</td><td>true</td><td>控制对应推送/演变轨是否可用；不停止 detect_signals 原始检测</td><td>§B §C2 §H</td></tr>
	<tr><td>ai_enabled / dry_run_ai</td><td>false / false</td><td>开启 AI 才会执行 L2/L3 调用；dry-run 只构造 payload</td><td>§E §F</td></tr>
	<tr><td>push_enabled</td><td>false</td><td>开启后才真实请求 Server酱；关闭时仍可记录理论推送</td><td>§J</td></tr>
	<tr><td>ai_conflict_guard</td><td>true</td><td>开启后 AI trade/spike 必须通过 scalp、pressure、guard 和压线检查</td><td>§H</td></tr>
	<tr><td>l2_require_volume_or_structure</td><td>true</td><td>开启时单 MACD 等弱 trade 信号不能轻易调用 AI</td><td>§E</td></tr>
	<tr><td>l3_local_spike_push</td><td>false</td><td>开启后强 scalp L3 可不依赖 AI 生成 spike，但微信要求额外 +10 分</td><td>§H §J</td></tr>
	<tr><td>forward_require_forecast_alignment</td><td>true</td><td>开启后 AI trade 与活跃结构演变反向时会被拦</td><td>§H</td></tr>
	<tr><td>calibration_enabled</td><td>true</td><td>开启事后样本、概率混合、低命中场景禁用及 AI 桶审计</td><td>§C2 §H §I</td></tr>
	<tr><td>calibration_min_samples</td><td>8</td><td>越高越晚开始相信历史桶</td><td>§C2 §I</td></tr>
	<tr><td>calibration_blend_weight</td><td>0.65</td><td>越高，历史命中率对 forecast 概率影响越大</td><td>§C2</td></tr>
	<tr><td>calibration_disable_below_hit_rate</td><td>0.38</td><td>历史命中率低于该值时场景/AI 桶可能被降级</td><td>§C2 §H</td></tr>
	<tr><td>paper_follow_ai_only / paper_fee_bps</td><td>true / 5</td><td>决定模拟账户是否只跟 AI，以及每次开平仓手续费</td><td>§I</td></tr>
	<tr><td>*_cooldown_seconds</td><td>trade/spike/watch 900；中线 spike 最短1800；reverse 300；forecast 1800</td><td>越高越不容易重复推送；中线不会因用户设置较低值而低于30分钟</td><td>§H §J</td></tr>
	</tbody></table>

	<h3>四种策略的权重差异</h3>
	<table class="help-table"><thead><tr><th>模式</th><th>更重视</th><th>更弱化</th><th>持仓参考</th></tr></thead><tbody>
	<tr><td>scalp</td><td>动量 1.4、量价 1.5、盘口 1.3</td><td>趋势 0.7、合约 0.7、高周期 0.5</td><td>3–15 分钟</td></tr>
	<tr><td>short</td><td>合约 1.15，其余大体均衡</td><td>盘口 0.8</td><td>15 分钟–数小时</td></tr>
	<tr><td>swing</td><td>趋势 1.4、合约 1.2、风险 1.3</td><td>动量/量价 0.7、盘口 0.3</td><td>数小时–数天</td></tr>
	<tr><td>long</td><td>趋势 1.6、风险 1.45</td><td>动量 0.55、量价 0.45、盘口 0.1</td><td>数天–数周</td></tr>
	</tbody></table>

	<h3>压测 KPI 必须分开看</h3>
	<table class="help-table"><thead><tr><th>指标</th><th>样本对象</th><th>命中逻辑</th><th>不能代表</th></tr></thead><tbody>
	<tr><td>ai_forward_direction_accuracy_pct</td><td>decision_source=ai 的 forward_view</td><td>按每条自己的 horizon 验证未来方向</td><td>不含未调用 AI 的轮次；不等于模拟收益</td></tr>
	<tr><td>prediction_accuracy_pct</td><td>AI开启时为合并 final_direction；AI关闭时为本地 score.final_direction</td><td>按界面所选验证窗口；观望要看后续是否未出现显著波动</td><td>AI关闭模式用于独立检验本地规则，不代表AI能力</td></tr>
	<tr><td>paper_pnl_pct</td><td>连续方向换仓账户</td><td>按价格路径、换仓时点和手续费累计</td><td>不是单条命中率；高准确率也可能因盈亏比差而亏损</td></tr>
	<tr><td>signal hit/fill rate</td><td>trade/spike 入场计划</td><td>先触达入场区，再看 5m/15m/1H 收益</td><td>不等于微信推送率</td></tr>
	<tr><td>forecast calibration hit rate</td><td>v2 策略级结构演变场景桶</td><td>目标结构严格确认，或窗口内最大顺向价格达到阈值</td><td>不等于最终 trade 胜率</td></tr>
	</tbody></table>
	<div class="help-note">出现“方向准确率不错但 paper PnL 为负”并不矛盾：可能正确的小波动很多、错误的大波动很少但损失更大，也可能频繁换仓手续费侵蚀收益。</div>

	<h3>运行环境变量</h3>
	<table class="help-table"><thead><tr><th>变量</th><th>默认</th><th>作用与边界</th></tr></thead><tbody>
	<tr><td>AI_CALL_MIN_INTERVAL_SECONDS</td><td>60（最低15）</td><td>超短/短线基础去重间隔；中线 L2 实际至少300秒，长线至少600秒；L3 使用基础间隔与事件去重</td></tr>
	<tr><td>AI_REQUEST_TIMEOUT</td><td>30s</td><td>正式 chat 单次超时</td></tr>
	<tr><td>AI_CIRCUIT_FAIL_THRESHOLD</td><td>3</td><td>连续失败达到后开熔断</td></tr>
	<tr><td>AI_CIRCUIT_COOLDOWN_SECONDS</td><td>120</td><td>open 状态冷却</td></tr>
	<tr><td>AI_PROBE_INTERVAL_SECONDS</td><td>60</td><td>half-open 探活最短间隔</td></tr>
	<tr><td>AI_ABNORMAL_ALERT_SECONDS</td><td>300</td><td>异常持续多久后发运维微信</td></tr>
	<tr><td>AI_ABNORMAL_ALERT_COOLDOWN_SECONDS</td><td>3600</td><td>同类运维告警冷却</td></tr>
	<tr><td>WEB_MONITOR_AUTO_RESTART</td><td>1</td><td>Web 监控子进程意外退出后自动拉起</td></tr>
	</tbody></table>

	<h3>调整参数时的推荐顺序</h3>
	<ol class="help-list">
	<li>先选 strategy_mode 和 risk_preference，决定你想捕捉哪种行情。</li>
	<li>再观察 §B 信号是否过多/过少，调整 volume、OI、费率和拥挤阈值。</li>
	<li>然后看 layer_scores、direction_guard 和 entry_plan，确认不是方向逻辑本身被卡住。</li>
	<li>最后调整 push_score 和 cooldown；不要用降低推送分数来掩盖结构判断不成立。</li>
	<li>积累足够样本后再根据 signal/forecast/AI 校准桶调权重，避免拿几条样本过拟合。</li>
	</ol>
	<h3>推送分数建议（配置页一键）</h3>
	<table class="help-table"><thead><tr><th>项</th><th>保守</th><th>标准</th><th>激进</th></tr></thead><tbody>
	<tr><td>push / short</td><td>80/78</td><td>75/75</td><td>70/68</td></tr>
	<tr><td>watch / spike / forecast</td><td>72/68/62</td><td>65/60/58</td><td>62/58/55</td></tr>
	</tbody></table>
	<div class="help-note">实现：<code>okx_signal_monitor.py</code> · 文档生成：<code>monitor_design_docs.py</code> · 以源码为准。</div>
	</section>

	</div></div>
	""",
    ]
    return "".join(parts)
