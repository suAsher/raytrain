import { useState } from "react";
import {
  Button,
  Card,
  Form,
  Input,
  Modal,
  Select,
  Space,
  Table,
  Tag,
  message,
} from "antd";
import { PlusOutlined } from "@ant-design/icons";
import { useDatasets, useRegisterDataset } from "../api/hooks";
import { errMsg } from "../api/client";
import type { Dataset } from "../api/types";

const visColor: Record<string, string> = {
  public: "green",
  tenant: "blue",
  private: "default",
};

export function DatasetsPage() {
  const { data, isLoading } = useDatasets();
  const reg = useRegisterDataset();
  const [open, setOpen] = useState(false);
  const [form] = Form.useForm();

  const onReg = async () => {
    const vals = await form.validateFields();
    try {
      await reg.mutateAsync({
        ...vals,
        tags: (vals.tags || "")
          .split(",")
          .map((s: string) => s.trim())
          .filter(Boolean),
      });
      message.success("数据集已注册");
      setOpen(false);
      form.resetFields();
    } catch (e) {
      message.error(errMsg(e));
    }
  };

  const columns = [
    { title: "名称", dataIndex: "name" },
    { title: "类型", dataIndex: "type", render: (t: string) => <Tag>{t}</Tag> },
    { title: "URI", dataIndex: "uri", ellipsis: true },
    {
      title: "可见性",
      dataIndex: "visibility",
      render: (v: string) => <Tag color={visColor[v]}>{v}</Tag>,
    },
    { title: "Owner", dataIndex: "owner" },
    {
      title: "行数",
      dataIndex: "rows",
      render: (n: number) => (n ? n.toLocaleString() : "-"),
    },
    {
      title: "Tags",
      dataIndex: "tags",
      render: (tags: string[]) => (
        <Space wrap>
          {(tags || []).map((t) => (
            <Tag key={t}>{t}</Tag>
          ))}
        </Space>
      ),
    },
  ];

  return (
    <Card
      title="数据集 (Lance)"
      extra={
        <Button
          type="primary"
          icon={<PlusOutlined />}
          onClick={() => setOpen(true)}
        >
          注册数据集
        </Button>
      }
    >
      <Table
        rowKey="id"
        loading={isLoading}
        dataSource={data || []}
        columns={columns}
        expandable={{
          expandedRowRender: (r: Dataset) => (
            <pre style={{ margin: 0 }}>
              {JSON.stringify(r.arrow_schema, null, 2)}
            </pre>
          ),
          rowExpandable: (r) => Object.keys(r.arrow_schema || {}).length > 0,
        }}
        locale={{ emptyText: "还没有可见的数据集" }}
      />

      <Modal
        title="注册 Lance 数据集"
        open={open}
        onOk={onReg}
        confirmLoading={reg.isPending}
        onCancel={() => setOpen(false)}
      >
        <Form
          form={form}
          layout="vertical"
          initialValues={{ type: "lance", visibility: "private" }}
        >
          <Form.Item name="name" label="名称" rules={[{ required: true }]}>
            <Input placeholder="nuscenes-v1-train" />
          </Form.Item>
          <Form.Item name="uri" label="URI" rules={[{ required: true }]}>
            <Input placeholder="s3://occ-lance/nuscenes_v1" />
          </Form.Item>
          <Form.Item name="type" label="类型">
            <Select
              options={[
                { label: "lance", value: "lance" },
                { label: "parquet", value: "parquet" },
                { label: "dir", value: "dir" },
              ]}
            />
          </Form.Item>
          <Form.Item name="visibility" label="可见性">
            <Select
              options={[
                { label: "private (仅自己)", value: "private" },
                { label: "tenant (团队)", value: "tenant" },
                { label: "public (全平台)", value: "public" },
              ]}
            />
          </Form.Item>
          <Form.Item name="tags" label="标签 (逗号分隔)">
            <Input placeholder="lidar, nuscenes" />
          </Form.Item>
          <Form.Item name="description" label="描述">
            <Input.TextArea rows={2} />
          </Form.Item>
        </Form>
      </Modal>
    </Card>
  );
}
