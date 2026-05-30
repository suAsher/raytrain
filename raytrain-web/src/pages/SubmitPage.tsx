import {
  Alert,
  Button,
  Card,
  Form,
  Input,
  InputNumber,
  Select,
  message,
} from "antd";
import { useNavigate } from "react-router-dom";
import { useSubmitJob, useDatasets, useMyQuota } from "../api/hooks";
import { errMsg } from "../api/client";

// Submit a training job. In the full flow the Workspace's code is packaged by
// the in-pod CLI; from the UI we submit with an explicit entrypoint and an
// optional pre-uploaded code_uri (left blank here — the server can also run a
// code-less submission against an image that already has the code).
export function SubmitPage() {
  const nav = useNavigate();
  const submit = useSubmitJob();
  const { data: datasets } = useDatasets();
  const { data: quota } = useMyQuota();
  const [form] = Form.useForm();

  const onFinish = async (vals: {
    repo: string;
    exp_name: string;
    gpu_type: string;
    num_nodes: number;
    gpus_per_node: number;
    entrypoint: string;
  }) => {
    try {
      const resp = await submit.mutateAsync(vals);
      message.success(`已提交: ${resp.submission_id}`);
      nav("/jobs");
    } catch (e) {
      message.error(errMsg(e));
    }
  };

  return (
    <Card title="提交训练" style={{ maxWidth: 720 }}>
      {quota && (
        <Alert
          type="info"
          showIcon
          style={{ marginBottom: 16 }}
          message={quotaBanner(quota)}
        />
      )}
      <Form
        form={form}
        layout="vertical"
        onFinish={onFinish}
        initialValues={{
          gpu_type: "h20",
          num_nodes: 1,
          gpus_per_node: 1,
          entrypoint: "python tools/train.py --config configs/x.py",
        }}
      >
        <Form.Item name="repo" label="项目名" rules={[{ required: true }]}>
          <Input placeholder="pointcept" />
        </Form.Item>
        <Form.Item name="exp_name" label="实验名" rules={[{ required: true }]}>
          <Input placeholder="smoke" />
        </Form.Item>
        <Form.Item name="gpu_type" label="GPU 类型">
          <Select
            options={[
              { label: "h20", value: "h20" },
              { label: "a100", value: "a100" },
            ]}
          />
        </Form.Item>
        <Form.Item name="num_nodes" label="节点数">
          <InputNumber min={1} max={64} style={{ width: "100%" }} />
        </Form.Item>
        <Form.Item name="gpus_per_node" label="每节点 GPU 数">
          <InputNumber min={0} max={8} style={{ width: "100%" }} />
        </Form.Item>
        <Form.Item
          label="数据集 (Lance)"
          tooltip="选中后会把 URI 注入训练环境变量 RAYTRAIN_DATA_SOURCE_URI"
        >
          <Select
            allowClear
            placeholder="可选：从注册表选一个 Lance 数据集"
            options={(datasets || []).map((d) => ({
              label: `${d.name} (${d.visibility})`,
              value: d.uri,
            }))}
          />
        </Form.Item>
        <Form.Item
          name="entrypoint"
          label="训练命令"
          rules={[{ required: true }]}
        >
          <Input.TextArea rows={2} />
        </Form.Item>
        <Form.Item>
          <Button type="primary" htmlType="submit" loading={submit.isPending}>
            提交训练
          </Button>
        </Form.Item>
      </Form>
    </Card>
  );
}

// Render a short "GPU 已用 X / 上限 Y（剩余 Z）" banner. cap<=0 means unlimited.
function quotaBanner(q: import("../api/types").MyQuota): string {
  const cap = q.quota.max_gpus;
  const used = q.usage.gpus;
  if (!cap || cap <= 0) {
    return `GPU 配额：不限（当前已用 ${used}）`;
  }
  const rem = q.remaining.gpus ?? Math.max(0, cap - used);
  return `GPU 配额：已用 ${used} / 上限 ${cap}（剩余 ${rem}）`;
}
