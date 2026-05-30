import { Layout, Menu, Dropdown, Avatar, Space, Typography } from "antd";
import {
  CloudServerOutlined,
  ExperimentOutlined,
  DatabaseOutlined,
  RocketOutlined,
  ThunderboltOutlined,
  UserOutlined,
  TeamOutlined,
  LogoutOutlined,
} from "@ant-design/icons";
import { Outlet, useLocation, useNavigate } from "react-router-dom";
import { clearToken } from "../api/client";
import { useWhoAmI } from "../api/hooks";

const { Header, Sider, Content } = Layout;

const NAV = [
  { key: "/workspaces", icon: <CloudServerOutlined />, label: "工作区" },
  { key: "/dev-sessions", icon: <ThunderboltOutlined />, label: "调试会话" },
  { key: "/submit", icon: <RocketOutlined />, label: "提交训练" },
  { key: "/jobs", icon: <ExperimentOutlined />, label: "任务" },
  { key: "/datasets", icon: <DatabaseOutlined />, label: "数据集" },
];

// Admin-only nav items, appended when the caller's role === "admin".
const ADMIN_NAV = [
  { key: "/admin/users", icon: <TeamOutlined />, label: "用户管理" },
];

export function AppLayout() {
  const nav = useNavigate();
  const loc = useLocation();
  const { data: me } = useWhoAmI();

  const navItems = me?.role === "admin" ? [...NAV, ...ADMIN_NAV] : NAV;

  const onLogout = () => {
    clearToken();
    nav("/login");
  };

  return (
    <Layout style={{ minHeight: "100vh" }}>
      <Sider theme="dark" breakpoint="lg" collapsedWidth="0">
        <div
          style={{
            color: "#fff",
            fontWeight: 700,
            fontSize: 18,
            padding: "16px 24px",
          }}
        >
          raytrain
        </div>
        <Menu
          theme="dark"
          mode="inline"
          selectedKeys={[loc.pathname]}
          items={navItems}
          onClick={(e) => nav(e.key)}
        />
      </Sider>
      <Layout>
        <Header
          style={{
            background: "#fff",
            display: "flex",
            justifyContent: "flex-end",
            alignItems: "center",
            paddingRight: 24,
          }}
        >
          <Dropdown
            menu={{
              items: [
                {
                  key: "logout",
                  icon: <LogoutOutlined />,
                  label: "退出登录",
                  onClick: onLogout,
                },
              ],
            }}
          >
            <Space style={{ cursor: "pointer" }}>
              <Avatar icon={<UserOutlined />} />
              <Typography.Text>
                {me ? `${me.user} (${me.role})` : "..."}
              </Typography.Text>
            </Space>
          </Dropdown>
        </Header>
        <Content style={{ margin: 24 }}>
          <Outlet />
        </Content>
      </Layout>
    </Layout>
  );
}
