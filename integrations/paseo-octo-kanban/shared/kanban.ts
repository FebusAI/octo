import { defineRpc } from "@getpaseo/plugin";
import { z } from "zod";

export const kanbanIssueSchema = z.object({
  number: z.number(),
  title: z.string(),
  state: z.string(),
  statusLabel: z.string(),
  typeLabel: z.string(),
  url: z.string(),
  activeClaim: z.string().nullable(),
  commentsCount: z.number(),
});

export const clusterNodeSchema = z.object({
  name: z.string(),
  host: z.string(),
  defaultProfile: z.string(),
  roles: z.array(z.string()),
  online: z.boolean().optional(),
  tools: z.array(z.string()).optional(),
  activeWorkers: z.number().optional(),
});

export const swarmMessageSchema = z.object({
  seq: z.number(),
  sender: z.string(),
  recipient: z.string(),
  text: z.string(),
  channel: z.string(),
  created_at: z.string(),
});

export const overlapReportSchema = z.object({
  level: z.string(),
  message: z.string(),
  conflicting_tasks: z.array(z.number()),
  conflicting_files: z.array(z.string()),
  suggested_action: z.string(),
});

export const coordinatorSchema = z.object({
  coordinator_id: z.string(),
  node_id: z.string(),
  active_epic: z.number().nullable(),
  goal_title: z.string(),
  owned_files: z.array(z.string()),
  heartbeat_at: z.string(),
});

export const boardStateSchema = z.object({
  eligible: z.boolean(),
  workspaceDir: z.string(),
  repo: z.string(),
  reason: z.string(),
  profiles: z.array(z.string()),
  issues: z.array(kanbanIssueSchema),
  nodes: z.array(clusterNodeSchema),
  swarmMessages: z.array(swarmMessageSchema),
  overlapReports: z.array(overlapReportSchema),
  coordinators: z.array(coordinatorSchema),
});

export const getKanbanBoardRpc = defineRpc({
  name: "octo.kanban.get",
  input: z.object({
    workspaceId: z.string().optional(),
    probeNodes: z.boolean().optional(),
  }),
  output: boardStateSchema,
});

export const runOctoActionRpc = defineRpc({
  name: "octo.action.run",
  input: z.object({
    workspaceId: z.string().optional(),
    action: z.enum([
      "init",
      "plan",
      "approve",
      "worker_once",
      "qa_once",
      "orchestrate_once",
      "paseo_spawn_worker",
      "node_add",
      "node_remove",
      "swarm_post",
      "c2c_send",
      "c2c_negotiate",
    ]),
    goal: z.string().optional(),
    issueNumber: z.number().optional(),
    profile: z.string().optional(),
    nodeName: z.string().optional(),
    messageText: z.string().optional(),
    recipient: z.string().optional(),
    channel: z.string().optional(),
    subject: z.string().optional(),
    kind: z.string().optional(),
  }),
  output: z.object({
    ok: z.boolean(),
    output: z.string(),
  }),
});
