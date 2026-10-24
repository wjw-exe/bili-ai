"""bili-ai：纯 headless 的 B 站客户端内核

分层：
  sign / http   —— 协议与传输（wbi 签名、连接复用、风控识别）
  api           —— 业务接口（search / view）
  store         —— 单一真相源（视图、列表、逻辑播放进度、命令日志）
  core          —— 命令总线（MCP / Web / CLI 共用）
  mcp_server    —— 面向 AI 的 stdio 工具面
  web           —— 面向人的可视化后台
"""

__version__ = "0.2.0"
