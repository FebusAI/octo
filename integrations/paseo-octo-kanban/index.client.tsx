import type {
  PluginButtonRegistration,
  PluginClientContext,
  PluginSidebarItemProps,
} from "@getpaseo/plugin/client";
import { SidebarRow } from "@getpaseo/plugin/client/ui";
import { OctoKanbanPanel, OctoKanbanScreen } from "./client/kanban";

function OctoSidebarItem({ currentScreen, openScreen }: PluginSidebarItemProps) {
  return (
    <SidebarRow
      icon="Kanban"
      active={currentScreen?.screenId === "octo-kanban"}
      onPress={() => openScreen({ screenId: "octo-kanban" })}
    />
  );
}

export default function contribute(client: PluginClientContext) {
  const removeScreen = client.addScreen({
    id: "octo-kanban",
    title: "Octo Kanban",
    Component: OctoKanbanScreen,
  });

  const removeSidebar = client.addSidebarHeaderItem({
    id: "octo-kanban",
    title: "Octo Kanban",
    Component: OctoSidebarItem,
  });

  const removePanel = client.addWorkspacePanel({
    id: "octo-kanban-panel",
    title: "Octo Kanban & Swarm",
    icon: "Kanban",
    context: "workspace",
    locations: ["workspace", "explorer"],
    Component: OctoKanbanPanel,
  });

  const removeCmd = client.addCommandCenterItem({
    id: "octo-kanban-cmd",
    title: "Apri Octo Multi-Agent Kanban Board",
    icon: "Kanban",
    context: "workspace",
    onSelect: ({ openPanel }) => openPanel("octo-kanban-panel"),
  });

  const lifetime = new AbortController();
  let releaseSubscription: (() => void) | undefined;
  const buttons = new Map<string, PluginButtonRegistration>();

  const upsertButton = (workspaceId: string) => {
    if (!buttons.has(workspaceId)) {
      buttons.set(
        workspaceId,
        client.addHeaderButton({
          id: "octo-kanban-btn",
          workspaceId,
          button: {
            title: "Octo Multi-Agent Kanban",
            icon: "Kanban",
            label: "Kanban",
            behavior: {
              kind: "action",
              onPress: () => client.openPanel("octo-kanban-panel", { workspaceId }),
            },
          },
        }),
      );
    }
  };

  const removeButton = (workspaceId: string) => {
    buttons.get(workspaceId)?.remove();
    buttons.delete(workspaceId);
  };

  void client.paseo.workspaces
    .list({ subscribe: {} })
    .then(({ subscription }) => {
      if (lifetime.signal.aborted) {
        void subscription.release();
        return;
      }
      releaseSubscription = () => {
        void subscription.release();
      };
      subscription.subscribe({
        snapshot: ({ entries }) => {
          for (const id of buttons.keys()) removeButton(id);
          for (const ws of entries) upsertButton(ws.id);
        },
        update: (msg) => {
          if (msg.type !== "workspace_update") return;
          const ev = msg.payload;
          if (ev.kind === "upsert") upsertButton(ev.workspace.id);
          else removeButton(ev.id);
        },
      });
    })
    .catch(() => {});

  return () => {
    lifetime.abort();
    releaseSubscription?.();
    for (const btn of buttons.values()) btn.remove();
    buttons.clear();
    removeCmd();
    removePanel();
    removeSidebar();
    removeScreen();
  };
}
