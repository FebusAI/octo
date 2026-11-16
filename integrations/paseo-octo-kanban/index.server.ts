import type { PluginServerContext } from "@getpaseo/plugin/server";
import { executeOctoAction, fetchKanbanBoard } from "./server/octo";
import { getKanbanBoardRpc, runOctoActionRpc } from "./shared/kanban";

export default function contribute(server: PluginServerContext) {
  server.handle(getKanbanBoardRpc, async (input, { paseo }) => {
    return await fetchKanbanBoard(paseo, input.workspaceId, Boolean(input.probeNodes));
  });

  server.handle(runOctoActionRpc, async (input, { paseo }) => {
    return await executeOctoAction(paseo, input);
  });

  return () => {};
}
