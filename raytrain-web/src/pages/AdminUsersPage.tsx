import { useState } from "react";
import {
  Button,
  Card,
  Form,
  Input,
  InputNumber,
  Modal,
  Select,
  Space,
  Switch,
  Table,
  Tag,
  Typography,
  message,
} from "antd";
import {
  useCreateUser,
  useDeleteUser,
  useUpdateUser,
  useUsers,
  type CreateUserBody,
} from "../api/hooks";
import { errMsg } from "../api/client";
import type { PlatformUser } from "../api/types";

const { Paragraph, Text } = Typography;

// Admin-only page: create platform users with per-user quota + grants, and
// update them later. Mirrors POST/PATCH/DELETE /v1/admin/users.
export function AdminUsersPage() {
  const { data, isLoading } = useUsers();
  const createUser = useCreateUser();
  const updateUser = useUpdateUser();
  const deleteUser = useDeleteUser();

  const [createOpen, setCreateOpen] = useState(false);
  const [editing, setEditing] = useState<PlatformUser | null>(null);
  const [createForm] = Form.useForm();
  const [editForm] = Form.useForm();

  const onCreate = async () => {
    const v = await createForm.validateFields();
    const body: CreateUserBody = {
      user: v.user,
      tenant: v.tenant || "default",
      role: v.role || "user",
      quota: {
        max_gpus: v.max_gpus ?? 0,
        max_jobs: v.max_jobs ?? 0,
        max_cpus: v.max_cpus ?? 0,
        max_memory_gi: v.max_memory_gi ?? 0,
      },
      projects: splitCsv(v.projects),
      datasets: splitCsv(v.datasets),
      image_prefixes: splitCsv(v.image_prefixes),
      issue_token: true,
      token_days: v.token_days ?? 365,
    };
    try {
      const res = await createUser.mutateAsync(body);
      setCreateOpen(false);
      createForm.resetFields();
      if (res.token) {
        Modal.success({
          title: `用户 ${res.user.user} 已创建`,
          width: 640,
          content: (
            <div>
              <Paragraph>把下面的访问令牌发给该用户（只显示这一次）：</Paragraph>
              <Paragraph copyable={{ text: res.token }}>
                <Text code style={{ wordBreak: "break-all" }}>
                  {res.token}
                </Text>
              </Paragraph>
            </div>
          ),
        });
      } else {
        message.success("用户已创建");
      }
    } catch (e) {
      message.error(errMsg(e));
    }
  };

  const onEdit = async () => {
    if (!editing) return;
    const v = await editForm.validateFields();
    try {
      await updateUser.mutateAsync({
        user: editing.user,
        body: {
          role: v.role,
          enabled: v.enabled,
          quota: {
            max_gpus: v.max_gpus ?? 0,
            max_jobs: v.max_jobs ?? 0,
            max_cpus: v.max_cpus ?? 0,
            max_memory_gi: v.max_memory_gi ?? 0,
          },
          projects: splitCsv(v.projects),
          datasets: splitCsv(v.datasets),
          image_prefixes: splitCsv(v.image_prefixes),
        },
      });
      setEditing(null);
      message.success("已更新");
    } catch (e) {
      message.error(errMsg(e));
    }
  };

  const columns = [
    { title: "用户", dataIndex: "user" },
    { title: "租户", dataIndex: "tenant" },
    {
      title: "角色",
      dataIndex: "role",
      render: (r: string) => (
        <Tag color={r === "admin" ? "gold" : "blue"}>{r}</Tag>
      ),
    },
    {
      title: "GPU 配额",
      render: (_: unknown, r: PlatformUser) => quotaLabel(r.quota.max_gpus),
    },
    {
      title: "并发任务",
      render: (_: unknown, r: PlatformUser) => quotaLabel(r.quota.max_jobs),
    },
    {
      title: "状态",
      render: (_: unknown, r: PlatformUser) =>
        r.enabled ? (
          <Tag color="green">启用</Tag>
        ) : (
          <Tag color="red">禁用</Tag>
        ),
    },
    {
      title: "操作",
      render: (_: unknown, r: PlatformUser) => (
        <Space>
          <a
            onClick={() => {
              setEditing(r);
              editForm.setFieldsValue({
                role: r.role,
                enabled: r.enabled,
                max_gpus: r.quota.max_gpus,
                max_jobs: r.quota.max_jobs,
                max_cpus: r.quota.max_cpus,
                max_memory_gi: r.quota.max_memory_gi,
                projects: (r.projects || []).join(", "),
                datasets: (r.datasets || []).join(", "),
                image_prefixes: (r.image_prefixes || []).join(", "),
              });
            }}
          >
            编辑
          </a>
          <a
            style={{ color: "red" }}
            onClick={() =>
              Modal.confirm({
                title: `删除用户 ${r.user}?`,
                content: "删除后其 token 立即失效。",
                okType: "danger",
                onOk: async () => {
                  try {
                    await deleteUser.mutateAsync(r.user);
                    message.success("已删除");
                  } catch (e) {
                    message.error(errMsg(e));
                  }
                },
              })
            }
          >
            删除
          </a>
        </Space>
      ),
    },
  ];

  return (
    <Card
      title="用户与配额管理"
      extra={
        <Button type="primary" onClick={() => setCreateOpen(true)}>
          创建用户
        </Button>
      }
    >
      <Paragraph type="secondary">
        在这里创建平台用户并分配 per-user 配额与授权；配额修改即时生效（无需重新发
        token）。配额为 0 表示不限制。
      </Paragraph>
      <Table
        rowKey="user"
        loading={isLoading}
        dataSource={data || []}
        columns={columns}
        locale={{ emptyText: "还没有用户，点右上角创建" }}
      />

      <Modal
        title="创建用户"
        open={createOpen}
        onCancel={() => setCreateOpen(false)}
        onOk={onCreate}
        confirmLoading={createUser.isPending}
        okText="创建并签发 token"
        width={560}
      >
        <Form form={createForm} layout="vertical">
          <Form.Item
            name="user"
            label="用户名"
            rules={[{ required: true, message: "必填，字母/数字/-/_" }]}
          >
            <Input placeholder="zhangsan" />
          </Form.Item>
          <Space size="large">
            <Form.Item name="tenant" label="租户" initialValue="default">
              <Input />
            </Form.Item>
            <Form.Item name="role" label="角色" initialValue="user">
              <Select
                style={{ width: 140 }}
                options={[
                  { label: "user", value: "user" },
                  { label: "admin", value: "admin" },
                ]}
              />
            </Form.Item>
            <Form.Item name="token_days" label="token 有效期(天)" initialValue={365}>
              <InputNumber min={1} />
            </Form.Item>
          </Space>
          <QuotaFields />
          <GrantFields />
        </Form>
      </Modal>

      <Modal
        title={editing ? `编辑用户 ${editing.user}` : ""}
        open={!!editing}
        onCancel={() => setEditing(null)}
        onOk={onEdit}
        confirmLoading={updateUser.isPending}
        okText="保存"
        width={560}
      >
        <Form form={editForm} layout="vertical">
          <Space size="large">
            <Form.Item name="role" label="角色">
              <Select
                style={{ width: 140 }}
                options={[
                  { label: "user", value: "user" },
                  { label: "admin", value: "admin" },
                ]}
              />
            </Form.Item>
            <Form.Item name="enabled" label="启用" valuePropName="checked">
              <Switch />
            </Form.Item>
          </Space>
          <QuotaFields />
          <GrantFields />
        </Form>
      </Modal>
    </Card>
  );
}

function QuotaFields() {
  return (
    <Space size="large" wrap>
      <Form.Item name="max_gpus" label="GPU 上限" initialValue={0}>
        <InputNumber min={0} />
      </Form.Item>
      <Form.Item name="max_jobs" label="并发任务上限" initialValue={0}>
        <InputNumber min={0} />
      </Form.Item>
      <Form.Item name="max_cpus" label="CPU 上限" initialValue={0}>
        <InputNumber min={0} />
      </Form.Item>
      <Form.Item name="max_memory_gi" label="内存上限(GiB)" initialValue={0}>
        <InputNumber min={0} />
      </Form.Item>
    </Space>
  );
}

function GrantFields() {
  return (
    <>
      <Form.Item name="projects" label="可用项目 (逗号分隔)">
        <Input placeholder="proj-a, proj-b" />
      </Form.Item>
      <Form.Item name="datasets" label="可用数据集 (逗号分隔)">
        <Input placeholder="scannet, nuscenes" />
      </Form.Item>
      <Form.Item name="image_prefixes" label="允许镜像前缀 (逗号分隔)">
        <Input placeholder="172.31.9.104:5050/training/" />
      </Form.Item>
    </>
  );
}

function splitCsv(s?: string): string[] {
  if (!s) return [];
  return s
    .split(",")
    .map((x) => x.trim())
    .filter(Boolean);
}

function quotaLabel(n: number): string {
  return !n || n <= 0 ? "不限" : String(n);
}
