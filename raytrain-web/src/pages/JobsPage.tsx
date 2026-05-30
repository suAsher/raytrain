import { useState } from "react";
import { Card, Select, Space, Table, Tag, message } from "antd";
import { useJobs, useStopJob } from "../api/hooks";
import type { JobInfo } from "../api/types";

const statusColor: Record<string, string> = {
  RUNNING: "green",
  PENDING: "blue",
  SUCCEEDED: "cyan",
  FAILED: "red",
  STOPPED: "default",
};

export function JobsPage() {
  const [gpuType, setGpuType] = useState("h20");
  const { data, isLoading } = useJobs(gpuType);
  const stop = useStopJob(gpuType);

  const columns = [
    { title: "提交 ID", dataIndex: "submission_id" },
    {
      title: "归属",
      render: (_: unknown, r: JobInfo) => r.metadata?.["raytrain.user"] || "-",
    },
    {
      title: "状态",
      dataIndex: "status",
      render: (s: string) => (
        <Tag color={statusColor[s] || "default"}>{s}</Tag>
      ),
    },
    {
      title: "操作",
      render: (_: unknown, r: JobInfo) => (
        <a
          style={{ color: "red" }}
          onClick={async () => {
            await stop.mutateAsync(r.submission_id);
            message.success("已发送停止");
          }}
        >
          停止
        </a>
      ),
    },
  ];

  return (
    <Card
      title="训练任务"
      extra={
        <Space>
          集群:
          <Select
            value={gpuType}
            style={{ width: 120 }}
            onChange={setGpuType}
            options={[
              { label: "h20", value: "h20" },
              { label: "a100", value: "a100" },
            ]}
          />
        </Space>
      }
    >
      <Table
        rowKey="submission_id"
        loading={isLoading}
        dataSource={data || []}
        columns={columns}
        locale={{ emptyText: "该集群暂无任务" }}
      />
    </Card>
  );
}
