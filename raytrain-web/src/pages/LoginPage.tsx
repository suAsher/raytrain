import { Button, Card, Form, Input, Typography, message } from "antd";
import { useNavigate } from "react-router-dom";
import { api, setToken } from "../api/client";
import type { WhoAmI } from "../api/types";

// M1 login = paste your platform token. M5+ swaps this for OIDC SSO.
export function LoginPage() {
  const nav = useNavigate();

  const onFinish = async (vals: { token: string }) => {
    setToken(vals.token.trim());
    try {
      const me = (await api.get<WhoAmI>("/v1/auth/me")).data;
      message.success(`欢迎, ${me.user}`);
      nav("/workspaces");
    } catch {
      message.error("token 无效或已过期");
    }
  };

  return (
    <div
      style={{
        minHeight: "100vh",
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        background: "#f0f2f5",
      }}
    >
      <Card style={{ width: 420 }}>
        <Typography.Title level={3} style={{ textAlign: "center" }}>
          raytrain 训练平台
        </Typography.Title>
        <Typography.Paragraph type="secondary" style={{ textAlign: "center" }}>
          粘贴管理员发给你的访问令牌
        </Typography.Paragraph>
        <Form layout="vertical" onFinish={onFinish}>
          <Form.Item
            name="token"
            label="访问令牌 (Token)"
            rules={[{ required: true, message: "请输入 token" }]}
          >
            <Input.Password placeholder="eyJhbGc..." />
          </Form.Item>
          <Form.Item>
            <Button type="primary" htmlType="submit" block>
              登录
            </Button>
          </Form.Item>
        </Form>
      </Card>
    </div>
  );
}
