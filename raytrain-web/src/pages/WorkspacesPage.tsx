import { useState } from "react";
import {
  Button,
  Card,
  Form,
  Input,
  InputNumber,
  Modal,
  Space,
  Table,
  Tag,
  message,
} from "antd";
import { PlusOutlined } from "@ant-design/icons";
import {
  useCreateWorkspace,
  useWorkspaceAction,
  useWorkspaces,
} from "../api/hooks";
import { errMsg } from "../api/client";
import type { Workspace } from "../api/types";

const stateColor: Record<string, string> = {
  running: "green",
  creating: "blue",
  stopped: "default",
  error: "red",
};

export function WorkspacesPage() {
  const { data, isLoading } = useWorkspaces();
  const create = useCreateWorkspace();
  const action = useWorkspaceAction();
  const [open, setOpen] = useState(false);
  const [form] = Form.useForm();

  const onCreate = async () => {
    const vals = await form.validateFields();
    try {
      await create.mutateAsync(vals);
      message.success("工作区创建中");
      setOpen(false);
      form.resetFields();
    } catch (e) {
      message.error(errMsg(e));
    }
  };

  const ideButtons = (ws: Workspace) => {
    const urls = ws.ide_urls || {};
    return (
      <Space wrap>
        {urls.code && (
          <a href={urls.code} target="_blank" rel="noreferrer">
            VS Code
          </a>
        )}
        {urls.jupyter && (
          <a href={urls.jupyter} target="_blank" rel="noreferrer">
            Jupyter
          </a>
        )}
        {urls.pycharm && (
          <a href={urls.pycharm} target="_blank" rel="noreferrer">
            PyCharm
          </a>
        )}
        {urls.ssh && (
          <span
            style={{ cursor: "pointer", color: "#1677ff" }}
            onClick={() => {
              navigator.clipboard?.writeText(urls.ssh);
              message.success("SSH 命令已复制");
            }}
          >
            SSH
          </span>
        )}
      </Space>
    );
  };

  const columns = [
    { title: "名称", dataIndex: "name" },
    {
      title: "状态",
      dataIndex: "state",
      render: (s: string, r: Workspace) => (
        <Space>
          <Tag color={stateColor[s] || "default"}>{s}</Tag>
          {r.pod_phase && <Tag>{r.pod_phase}</Tag>}
        </Space>
      ),
    },
    {
      title: "规格",
      render: (_: unknown, r: Workspace) =>
        `${r.cpu}C ${r.memory_gi}G / PVC ${r.pvc_gi}Gi`,
    },
    { title: "IDE 入口", render: (_: unknown, r: Workspace) => ideButtons(r) },
    {
      title: "操作",
      render: (_: unknown, r: Workspace) => (
        <Space>
          {r.state === "stopped" ? (
            <a onClick={() => action.mutate({ id: r.id, action: "start" })}>
              启动
            </a>
          ) : (
            <a onClick={() => action.mutate({ id: r.id, action: "stop" })}>
              停止
            </a>
          )}
          <a
            style={{ color: "red" }}
            onClick={() =>
              Modal.confirm({
                title: `删除工作区 ${r.name}?`,
                content: "PVC 会一并删除，代码将丢失。",
                onOk: () => action.mutate({ id: r.id, action: "delete" }),
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
      title="我的工作区"
      extra={
        <Button
          type="primary"
          icon={<PlusOutlined />}
          onClick={() => setOpen(true)}
        >
          创建工作区
        </Button>
      }
    >
      <Table
        rowKey="id"
        loading={isLoading}
        dataSource={data || []}
        columns={columns}
        locale={{ emptyText: "还没有工作区，点右上角创建一个" }}
      />

      <Modal
        title="创建工作区"
        open={open}
        onOk={onCreate}
        confirmLoading={create.isPending}
        onCancel={() => setOpen(false)}
      >
        <Form form={form} layout="vertical" initialValues={{ cpu: 4, memory_gi: 8, pvc_gi: 100 }}>
          <Form.Item name="name" label="名称" rules={[{ required: true }]}>
            <Input placeholder="my-pointcept" />
          </Form.Item>
          <Form.Item name="cpu" label="CPU 核">
            <InputNumber min={1} max={64} style={{ width: "100%" }} />
          </Form.Item>
          <Form.Item name="memory_gi" label="内存 (Gi)">
            <InputNumber min={1} max={512} style={{ width: "100%" }} />
          </Form.Item>
          <Form.Item name="pvc_gi" label="存储 (Gi)">
            <InputNumber min={10} max={2000} style={{ width: "100%" }} />
          </Form.Item>
        </Form>
      </Modal>
    </Card>
  );
}
