import { useState } from "react";
import {
  Button,
  Card,
  Form,
  InputNumber,
  Modal,
  Select,
  Space,
  Table,
  Tag,
  message,
} from "antd";
import { PlusOutlined } from "@ant-design/icons";
import {
  useCreateDevSession,
  useDeleteDevSession,
  useDevSessions,
  useWorkspaces,
} from "../api/hooks";
import { errMsg } from "../api/client";
import type { DevSession } from "../api/types";

export function DevSessionsPage() {
  const { data, isLoading } = useDevSessions();
  const { data: workspaces } = useWorkspaces();
  const create = useCreateDevSession();
  const del = useDeleteDevSession();
  const [open, setOpen] = useState(false);
  const [form] = Form.useForm();

  const onCreate = async () => {
    const vals = await form.validateFields();
    try {
      await create.mutateAsync(vals);
      message.success("调试会话申请中（GPU 资源调度需几十秒）");
      setOpen(false);
      form.resetFields();
    } catch (e) {
      message.error(errMsg(e));
    }
  };

  const columns = [
    { title: "会话 ID", dataIndex: "id" },
    {
      title: "GPU",
      render: (_: unknown, r: DevSession) => `${r.gpu_count} × ${r.gpu_type}`,
    },
    {
      title: "状态",
      dataIndex: "state",
      render: (s: string, r: DevSession) => (
        <Space>
          <Tag color={s === "running" ? "green" : "blue"}>{s}</Tag>
          {r.pod_phase && <Tag>{r.pod_phase}</Tag>}
        </Space>
      ),
    },
    {
      title: "IDE 入口",
      render: (_: unknown, r: DevSession) => {
        const u = r.ide_urls || {};
        return (
          <Space>
            {u.code && (
              <a href={u.code} target="_blank" rel="noreferrer">
                VS Code
              </a>
            )}
            {u.jupyter && (
              <a href={u.jupyter} target="_blank" rel="noreferrer">
                Jupyter
              </a>
            )}
          </Space>
        );
      },
    },
    {
      title: "操作",
      render: (_: unknown, r: DevSession) => (
        <a style={{ color: "red" }} onClick={() => del.mutate(r.id)}>
          终止
        </a>
      ),
    },
  ];

  return (
    <Card
      title="调试会话 (GPU)"
      extra={
        <Button
          type="primary"
          icon={<PlusOutlined />}
          onClick={() => setOpen(true)}
        >
          申请 GPU
        </Button>
      }
    >
      <Table
        rowKey="id"
        loading={isLoading}
        dataSource={data || []}
        columns={columns}
        locale={{ emptyText: "没有活跃的调试会话" }}
      />

      <Modal
        title="申请 GPU 调试会话"
        open={open}
        onOk={onCreate}
        confirmLoading={create.isPending}
        onCancel={() => setOpen(false)}
      >
        <Form form={form} layout="vertical" initialValues={{ gpu_type: "h20", gpu_count: 1 }}>
          <Form.Item
            name="workspace_id"
            label="关联工作区 (共享代码)"
            rules={[{ required: true }]}
          >
            <Select
              placeholder="选择一个工作区"
              options={(workspaces || []).map((w) => ({
                label: w.name,
                value: w.id,
              }))}
            />
          </Form.Item>
          <Form.Item name="gpu_type" label="GPU 类型">
            <Select
              options={[
                { label: "h20", value: "h20" },
                { label: "a100", value: "a100" },
              ]}
            />
          </Form.Item>
          <Form.Item name="gpu_count" label="GPU 数量">
            <InputNumber min={1} max={8} style={{ width: "100%" }} />
          </Form.Item>
        </Form>
      </Modal>
    </Card>
  );
}
