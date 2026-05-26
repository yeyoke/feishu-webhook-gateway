# Jenkins 飞书 Webhook 网关

这是一个轻量 Python 服务，用来接收 Jenkins Pipeline 的发版通知，并转换成飞书自定义机器人支持的消息格式后发送到群 webhook。

## 启动

只需要 Python 3.10+，不依赖第三方包。

```powershell
python app.py -c config.example.json
```

如果配置文件里不写 `webhook_url`，会自动读取环境变量：

```powershell
$env:FEISHU_WEBHOOK_URL="https://open.feishu.cn/open-apis/bot/v2/hook/xxx"
$env:FEISHU_SECRET="可选，飞书机器人签名密钥"
$env:GATEWAY_TOKEN="change-me"
python app.py -c config.example.json
```

健康检查：

```bash
curl http://127.0.0.1:8008/healthz
```

## Jenkins 调用方式

Jenkins 只需要向网关发送普通 JSON。网关只配置一个飞书 webhook，不需要区分环境或传 `target`。

```groovy
pipeline {
  agent any

  stages {
    stage('Deploy') {
      steps {
        echo 'deploying...'
      }
    }
  }

  post {
    always {
      script {
        def payload = groovy.json.JsonOutput.toJson([
          title: '开始发布',
          project: env.JOB_NAME,
        ])

        httpRequest(
          httpMode: 'POST',
          url: 'http://webhook-gateway.example.com:8008/notify',
          customHeaders: [[name: 'Authorization', value: 'Bearer change-me']],
          contentType: 'APPLICATION_JSON',
          requestBody: payload,
          validResponseCodes: '200:299'
        )
      }
    }
  }
}
```

如果 Jenkins 没装 `HTTP Request Plugin`，也可以用 `curl`：

```groovy
bat '''
curl -X POST http://webhook-gateway.example.com:8008/notify ^
  -H "Authorization: Bearer change-me" ^
  -H "Content-Type: application/json" ^
  -d "{\\"title\\":\\"发版通知\\",\\"project\\":\\"%JOB_NAME%\\",\\"status\\":\\"%BUILD_STATUS%\\",\\"build_url\\":\\"%BUILD_URL%\\"}"
'''
```

## 请求字段

网关会识别这些字段：

| 字段 | 说明 |
| --- | --- |
| `title` | 消息标题 |
| `project` | Jenkins 项目名 |

## 安全配置

- `request_token` 或 `GATEWAY_TOKEN`：保护网关接口，Jenkins 请求需要带 `Authorization: Bearer <token>`。
- `secret` 或 `FEISHU_SECRET`：飞书自定义机器人启用“签名校验”时填写。
