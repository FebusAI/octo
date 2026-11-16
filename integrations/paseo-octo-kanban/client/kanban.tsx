import { useRpc, type PluginScreenProps, type PluginWorkspacePanelProps } from "@getpaseo/plugin/client";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Pressable, ScrollView, Text, TextInput, View } from "react-native";
import { getKanbanBoardRpc, runOctoActionRpc } from "../shared/kanban";
import { openExternal } from "./web";

type PluginTheme = PluginScreenProps["theme"];

const KANBAN_COLUMNS = [
  { key: "status:awaiting-approval", title: "⏸️ Awaiting Approval", badgeColor: "#FBCA04" },
  { key: "status:ready", title: "🟢 Ready (Backlog)", badgeColor: "#0E8A16" },
  { key: "status:blocked", title: "🔴 Blocked (DAG)", badgeColor: "#B60205" },
  { key: "status:in-progress", title: "🔵 In Progress (Leased)", badgeColor: "#1D76DB" },
  { key: "status:in-review", title: "🟣 In QA Review", badgeColor: "#5319E7" },
  { key: "status:rework", title: "🟠 Rework Needed", badgeColor: "#E99695" },
  { key: "status:done", title: "✅ Done & Merged", badgeColor: "#006B75" },
];

function KanbanDashboard({
  theme,
  compact,
  workspaceId,
}: {
  theme: PluginTheme;
  compact: boolean;
  workspaceId?: string;
}) {
  const c = theme.colors;
  const queryClient = useQueryClient();
  const fetchBoard = useRpc(getKanbanBoardRpc);
  const runAction = useRpc(runOctoActionRpc);

  const [activeTab, setActiveTab] = useState<"kanban" | "swarm" | "multiplayer">("kanban");
  const [probeNodes, setProbeNodes] = useState(false);
  const [selectedProfile, setSelectedProfile] = useState("worker");
  const [newGoal, setNewGoal] = useState("");
  const [swarmMsg, setSwarmMsg] = useState("");
  const [swarmRecipient, setSwarmRecipient] = useState("@all");
  const [swarmChannel, setSwarmChannel] = useState("main");
  const [busy, setBusy] = useState(false);
  const [actionLog, setActionLog] = useState("");

  const board = useQuery({
    queryKey: ["octo-kanban-board", workspaceId, probeNodes],
    queryFn: () => fetchBoard({ workspaceId, probeNodes }),
    refetchInterval: 8000,
  });

  const refresh = () => {
    void queryClient.invalidateQueries({ queryKey: ["octo-kanban-board"] });
  };

  async function triggerAction(payload: Parameters<typeof runAction>[0]) {
    setBusy(true);
    setActionLog("⏳ Esecuzione in corso...");
    try {
      const res = await runAction({ workspaceId, ...payload });
      setActionLog((res.ok ? "✅ " : "❌ ") + res.output);
      refresh();
    } catch (err: any) {
      setActionLog("❌ Errore: " + (err?.message || String(err)));
    } finally {
      setBusy(false);
    }
  }

  const profiles = board.data?.profiles?.length
    ? board.data.profiles
    : ["planner-architect", "worker", "qa", "claude-cli", "pi-local"];

  const issues = board.data?.issues || [];
  const nodes = board.data?.nodes || [];
  const swarmMessages = board.data?.swarmMessages || [];
  const overlapReports = board.data?.overlapReports || [];
  const coordinators = board.data?.coordinators || [];

  const cardStyle = {
    borderWidth: 1,
    borderColor: c.foregroundMuted,
    borderRadius: 10,
    padding: 14,
    backgroundColor: c.surface0,
    gap: 10,
  };

  const btnStyle = (primary = true) => ({
    paddingVertical: 9,
    paddingHorizontal: 13,
    borderRadius: 8,
    backgroundColor: primary ? c.accent : c.surface0,
    borderWidth: primary ? 0 : 1,
    borderColor: c.foregroundMuted,
    opacity: busy ? 0.5 : 1,
  });

  return (
    <ScrollView
      contentContainerStyle={{
        padding: compact ? 12 : 22,
        gap: 16,
        backgroundColor: c.surface0,
        flexGrow: 1,
      }}
    >
      {/* Header */}
      <View style={{ gap: 4 }}>
        <Text accessibilityRole="header" style={{ color: c.foreground, fontSize: 22, fontWeight: "700" }}>
          🐙 Octo Multiplayer Swarm Hub
        </Text>
        <Text selectable style={{ color: c.foregroundMuted, fontSize: 13 }}>
          Swarm Board & Multiplayer Overlap Radar · Repo: {board.data?.repo || "local/repo"}
        </Text>
      </View>

      {/* Navigation Tabs */}
      <View style={{ flexDirection: "row", gap: 8, borderBottomWidth: 1, borderColor: c.foregroundMuted, paddingBottom: 8 }}>
        <Pressable
          accessibilityRole="button"
          onPress={() => setActiveTab("kanban")}
          style={{
            paddingVertical: 6,
            paddingHorizontal: 12,
            borderRadius: 6,
            backgroundColor: activeTab === "kanban" ? c.accent : c.surface0,
          }}
        >
          <Text style={{ color: activeTab === "kanban" ? c.accentForeground : c.foreground, fontWeight: "600" }}>
            📋 Kanban Board ({issues.length})
          </Text>
        </Pressable>

        <Pressable
          accessibilityRole="button"
          onPress={() => setActiveTab("swarm")}
          style={{
            paddingVertical: 6,
            paddingHorizontal: 12,
            borderRadius: 6,
            backgroundColor: activeTab === "swarm" ? c.accent : c.surface0,
          }}
        >
          <Text style={{ color: activeTab === "swarm" ? c.accentForeground : c.foreground, fontWeight: "600" }}>
            🐝 Swarm Message Board ({swarmMessages.length})
          </Text>
        </Pressable>

        <Pressable
          accessibilityRole="button"
          onPress={() => setActiveTab("multiplayer")}
          style={{
            paddingVertical: 6,
            paddingHorizontal: 12,
            borderRadius: 6,
            backgroundColor: activeTab === "multiplayer" ? c.accent : c.surface0,
          }}
        >
          <Text style={{ color: activeTab === "multiplayer" ? c.accentForeground : c.foreground, fontWeight: "600" }}>
            🌐 Multiplayer Radar ({overlapReports.length > 0 ? `⚠️ ${overlapReports.length} Overlaps` : "🟢 Safe"})
          </Text>
        </Pressable>
      </View>

      {/* Action Bar */}
      <View style={{ flexDirection: "row", flexWrap: "wrap", gap: 8 }}>
        <Pressable accessibilityRole="button" disabled={busy} onPress={() => refresh()} style={btnStyle(false)}>
          <Text style={{ color: c.foreground, fontWeight: "600" }}>🔄 Refresh</Text>
        </Pressable>

        <Pressable
          accessibilityRole="button"
          disabled={busy}
          onPress={() => triggerAction({ action: "paseo_spawn_worker", profile: selectedProfile })}
          style={btnStyle(true)}
        >
          <Text style={{ color: c.accentForeground, fontWeight: "600" }}>
            🚀 Lancia Agente Paseo ({selectedProfile})
          </Text>
        </Pressable>

        <Pressable
          accessibilityRole="button"
          disabled={busy}
          onPress={() => triggerAction({ action: "worker_once", profile: selectedProfile })}
          style={btnStyle(false)}
        >
          <Text style={{ color: c.foreground, fontWeight: "600" }}>⚙️ Worker 1-Step</Text>
        </Pressable>

        <Pressable
          accessibilityRole="button"
          disabled={busy}
          onPress={() => triggerAction({ action: "orchestrate_once", profile: selectedProfile })}
          style={btnStyle(false)}
        >
          <Text style={{ color: c.foreground, fontWeight: "600" }}>🛡️ QA Gate</Text>
        </Pressable>

        <Pressable
          accessibilityRole="button"
          disabled={busy}
          onPress={() => triggerAction({ action: "c2c_negotiate" })}
          style={btnStyle(false)}
        >
          <Text style={{ color: c.foreground, fontWeight: "600" }}>🤝 C2C Auto-Negotiate</Text>
        </Pressable>
      </View>

      {/* Harness / Model Selector */}
      <View style={cardStyle}>
        <Text style={{ color: c.foreground, fontWeight: "700", fontSize: 13 }}>
          🎛️ Agent / Harness Profile Selector
        </Text>
        <View style={{ flexDirection: "row", flexWrap: "wrap", gap: 6 }}>
          {profiles.map((prof) => {
            const active = prof === selectedProfile;
            return (
              <Pressable
                key={prof}
                accessibilityRole="button"
                onPress={() => setSelectedProfile(prof)}
                style={{
                  paddingVertical: 5,
                  paddingHorizontal: 9,
                  borderRadius: 6,
                  backgroundColor: active ? c.accent : c.surface0,
                  borderWidth: 1,
                  borderColor: active ? c.accent : c.foregroundMuted,
                }}
              >
                <Text style={{ color: active ? c.accentForeground : c.foreground, fontWeight: active ? "700" : "400", fontSize: 12 }}>
                  {prof}
                </Text>
              </Pressable>
            );
          })}
        </View>
      </View>

      {/* Action Output Console */}
      {!!actionLog && (
        <View style={cardStyle}>
          <Text style={{ color: c.foreground, fontWeight: "700", fontSize: 13 }}>📟 Console Octo</Text>
          <Text selectable style={{ color: c.foreground, fontFamily: "monospace", fontSize: 12 }}>
            {actionLog}
          </Text>
        </View>
      )}

      {/* TAB 1: KANBAN BOARD */}
      {activeTab === "kanban" && (
        <View style={{ gap: 14 }}>
          {/* Phase 1 Planning Box */}
          <View style={cardStyle}>
            <Text style={{ color: c.foreground, fontWeight: "700" }}>
              📐 Fase 1: Nuovo Planning (Crea Epic in status:awaiting-approval)
            </Text>
            <TextInput
              placeholder="Descrivi il Goal architetturale da pianificare..."
              placeholderTextColor={c.foregroundMuted}
              editable={!busy}
              value={newGoal}
              onChangeText={setNewGoal}
              style={{
                borderWidth: 1,
                borderColor: c.foregroundMuted,
                borderRadius: 8,
                padding: 10,
                color: c.foreground,
              }}
            />
            <Pressable
              accessibilityRole="button"
              disabled={busy || !newGoal.trim()}
              onPress={() => {
                const g = newGoal;
                setNewGoal("");
                void triggerAction({ action: "plan", goal: g, profile: selectedProfile });
              }}
              style={btnStyle(true)}
            >
              <Text style={{ color: c.accentForeground, textAlign: "center", fontWeight: "600" }}>
                🧠 Genera Piano & DAG su GitHub
              </Text>
            </Pressable>
          </View>

          {/* Kanban Columns */}
          {KANBAN_COLUMNS.map((col) => {
            const colIssues = issues.filter((i) => i.statusLabel === col.key);
            return (
              <View key={col.key} style={cardStyle}>
                <Text style={{ color: c.foreground, fontWeight: "700", fontSize: 14 }}>
                  {col.title} ({colIssues.length})
                </Text>
                {colIssues.length === 0 ? (
                  <Text style={{ color: c.foregroundMuted, fontSize: 12 }}>Nessuna issue in questo stato.</Text>
                ) : (
                  colIssues.map((iss) => (
                    <View
                      key={iss.number}
                      style={{
                        padding: 10,
                        borderWidth: 1,
                        borderColor: c.foregroundMuted,
                        borderRadius: 8,
                        gap: 6,
                      }}
                    >
                      <Text selectable style={{ color: c.foreground, fontWeight: "600" }}>
                        #{iss.number} · {iss.title}
                      </Text>
                      <Text style={{ color: c.foregroundMuted, fontSize: 12 }}>
                        Tipo: {iss.typeLabel} · Commenti: {iss.commentsCount}
                        {iss.activeClaim ? ` · 🔒 Lease: ${iss.activeClaim}` : ""}
                      </Text>
                      <View style={{ flexDirection: "row", flexWrap: "wrap", gap: 8 }}>
                        {iss.statusLabel === "status:awaiting-approval" && (
                          <Pressable
                            accessibilityRole="button"
                            disabled={busy}
                            onPress={() => triggerAction({ action: "approve", issueNumber: iss.number })}
                            style={btnStyle(true)}
                          >
                            <Text style={{ color: c.accentForeground, fontWeight: "600", fontSize: 12 }}>
                              ✅ Approva Epic #{iss.number} & Esplodi DAG
                            </Text>
                          </Pressable>
                        )}

                        {(iss.statusLabel === "status:ready" || iss.statusLabel === "status:rework") && (
                          <Pressable
                            accessibilityRole="button"
                            disabled={busy}
                            onPress={() =>
                              triggerAction({
                                action: "paseo_spawn_worker",
                                issueNumber: iss.number,
                                profile: selectedProfile,
                              })
                            }
                            style={btnStyle(true)}
                          >
                            <Text style={{ color: c.accentForeground, fontWeight: "600", fontSize: 12 }}>
                              🤖 Lavora su #{iss.number} in Paseo
                            </Text>
                          </Pressable>
                        )}

                        {iss.statusLabel === "status:in-review" && (
                          <Pressable
                            accessibilityRole="button"
                            disabled={busy}
                            onPress={() =>
                              triggerAction({
                                action: "qa_once",
                                issueNumber: iss.number,
                                profile: selectedProfile,
                              })
                            }
                            style={btnStyle(true)}
                          >
                            <Text style={{ color: c.accentForeground, fontWeight: "600", fontSize: 12 }}>
                              🛡️ Esegui QA su #{iss.number}
                            </Text>
                          </Pressable>
                        )}

                        {!!iss.url && (
                          <Pressable accessibilityRole="link" onPress={() => openExternal(iss.url)} style={btnStyle(false)}>
                            <Text style={{ color: c.foreground, fontSize: 12 }}>🔗 GitHub</Text>
                          </Pressable>
                        )}
                      </View>
                    </View>
                  ))
                )}
              </View>
            );
          })}
        </View>
      )}

      {/* TAB 2: LIVE SWARM MESSAGE BOARD */}
      {activeTab === "swarm" && (
        <View style={{ gap: 14 }}>
          {/* Post Message Composer */}
          <View style={cardStyle}>
            <Text style={{ color: c.foreground, fontWeight: "700", fontSize: 14 }}>
              💬 Pubblica Messaggio sulla Swarm Board
            </Text>
            <View style={{ flexDirection: "row", gap: 8 }}>
              <TextInput
                placeholder="Destinatario (@parser, @verifier, @all, @coordinator)"
                placeholderTextColor={c.foregroundMuted}
                value={swarmRecipient}
                onChangeText={setSwarmRecipient}
                style={{
                  flex: 1,
                  borderWidth: 1,
                  borderColor: c.foregroundMuted,
                  borderRadius: 6,
                  padding: 8,
                  color: c.foreground,
                  fontSize: 12,
                }}
              />
              <TextInput
                placeholder="Canale (main, c2c, qa)"
                placeholderTextColor={c.foregroundMuted}
                value={swarmChannel}
                onChangeText={setSwarmChannel}
                style={{
                  width: 100,
                  borderWidth: 1,
                  borderColor: c.foregroundMuted,
                  borderRadius: 6,
                  padding: 8,
                  color: c.foreground,
                  fontSize: 12,
                }}
              />
            </View>
            <TextInput
              placeholder="Messaggio per i peer worker o verifier..."
              placeholderTextColor={c.foregroundMuted}
              value={swarmMsg}
              onChangeText={setSwarmMsg}
              style={{
                borderWidth: 1,
                borderColor: c.foregroundMuted,
                borderRadius: 8,
                padding: 10,
                color: c.foreground,
              }}
            />
            <Pressable
              accessibilityRole="button"
              disabled={busy || !swarmMsg.trim()}
              onPress={() => {
                const txt = swarmMsg;
                setSwarmMsg("");
                void triggerAction({
                  action: "swarm_post",
                  messageText: txt,
                  recipient: swarmRecipient,
                  channel: swarmChannel,
                });
              }}
              style={btnStyle(true)}
            >
              <Text style={{ color: c.accentForeground, textAlign: "center", fontWeight: "600" }}>
                🚀 Invia sulla Swarm Board
              </Text>
            </Pressable>
          </View>

          {/* Messages Stream */}
          <View style={cardStyle}>
            <Text style={{ color: c.foreground, fontWeight: "700", fontSize: 14 }}>
              📜 Stream Messaggi Swarm ({swarmMessages.length})
            </Text>
            {swarmMessages.length === 0 ? (
              <Text style={{ color: c.foregroundMuted, fontSize: 12 }}>Nessun messaggio sulla board.</Text>
            ) : (
              swarmMessages.map((m) => (
                <View
                  key={m.seq}
                  style={{
                    padding: 8,
                    borderBottomWidth: 1,
                    borderColor: c.foregroundMuted,
                    gap: 3,
                  }}
                >
                  <View style={{ flexDirection: "row", justifyContent: "space-between" }}>
                    <Text style={{ color: c.foreground, fontWeight: "700", fontSize: 12 }}>
                      #{m.seq} [{m.channel}] {m.sender} ➔ {m.recipient}
                    </Text>
                    <Text style={{ color: c.foregroundMuted, fontSize: 11 }}>
                      {m.created_at.slice(11, 19)} UTC
                    </Text>
                  </View>
                  <Text selectable style={{ color: c.foreground, fontSize: 13 }}>
                    {m.text}
                  </Text>
                </View>
              ))
            )}
          </View>
        </View>
      )}

      {/* TAB 3: MULTIPLAYER RADAR & C2C HUB */}
      {activeTab === "multiplayer" && (
        <View style={{ gap: 14 }}>
          {/* Overlap Radar */}
          <View style={cardStyle}>
            <Text style={{ color: c.foreground, fontWeight: "700", fontSize: 14 }}>
              🔍 Collision & Dependency Radar
            </Text>
            {overlapReports.length === 0 ? (
              <View style={{ padding: 10, borderRadius: 8, backgroundColor: "#0E8A1620" }}>
                <Text style={{ color: "#0E8A16", fontWeight: "600" }}>
                  ✅ Zero Collisioni: tutti i worker e i rami attivi lavorano su file separati!
                </Text>
              </View>
            ) : (
              overlapReports.map((r, idx) => (
                <View
                  key={idx}
                  style={{
                    padding: 10,
                    borderRadius: 8,
                    backgroundColor: r.level === "CRITICAL_COLLISION" ? "#B6020520" : "#FBCA0420",
                    borderWidth: 1,
                    borderColor: r.level === "CRITICAL_COLLISION" ? "#B60205" : "#FBCA04",
                    gap: 4,
                  }}
                >
                  <Text style={{ color: c.foreground, fontWeight: "700" }}>
                    {r.level === "CRITICAL_COLLISION" ? "🚨 COLLISIONE DIRETTA" : "⚠️ DIPENDENZA D'INTERFACCIA"}
                  </Text>
                  <Text style={{ color: c.foreground, fontSize: 13 }}>{r.message}</Text>
                  <Text style={{ color: c.foregroundMuted, fontSize: 12 }}>
                    File: {r.conflicting_files.join(", ")}
                  </Text>
                  <Text style={{ color: c.foreground, fontSize: 12, fontStyle: "italic" }}>
                    💡 Azione suggerita: {r.suggested_action}
                  </Text>
                </View>
              ))
            )}
          </View>

          {/* Active Coordinators */}
          <View style={cardStyle}>
            <Text style={{ color: c.foreground, fontWeight: "700", fontSize: 14 }}>
              🌐 Coordinatori Attivi nel Cluster ({coordinators.length})
            </Text>
            {coordinators.length === 0 ? (
              <Text style={{ color: c.foregroundMuted, fontSize: 12 }}>
                Nessun coordinatore esterno attivo. Registra il primo avviando una Epic!
              </Text>
            ) : (
              coordinators.map((coord) => (
                <View
                  key={coord.coordinator_id}
                  style={{
                    padding: 10,
                    borderWidth: 1,
                    borderColor: c.foregroundMuted,
                    borderRadius: 8,
                    gap: 4,
                  }}
                >
                  <Text style={{ color: c.foreground, fontWeight: "700" }}>
                    {coord.coordinator_id} (Nodo: {coord.node_id})
                  </Text>
                  <Text style={{ color: c.foregroundMuted, fontSize: 12 }}>
                    Epic Attiva: {coord.active_epic ? `#${coord.active_epic} (${coord.goal_title})` : "Idle"}
                  </Text>
                  <Text style={{ color: c.foregroundMuted, fontSize: 12 }}>
                    File Presidiati: {coord.owned_files.join(", ") || "Nessuno"}
                  </Text>
                </View>
              ))
            )}
          </View>

          {/* Multi-PC Nodes List */}
          <View style={cardStyle}>
            <View style={{ flexDirection: "row", justifyContent: "space-between", alignItems: "center" }}>
              <Text style={{ color: c.foreground, fontSize: 14, fontWeight: "700" }}>
                🖥️ Nodi Ephemeral Multi-PC (SSH Mesh)
              </Text>
              <Pressable accessibilityRole="button" onPress={() => setProbeNodes((v) => !v)} style={btnStyle(false)}>
                <Text style={{ color: c.foreground, fontSize: 11 }}>
                  {probeNodes ? "🟢 Probe Attivo" : "📡 Esegui Probe SSH"}
                </Text>
              </Pressable>
            </View>

            {nodes.map((node) => (
              <View
                key={node.name}
                style={{
                  padding: 8,
                  borderWidth: 1,
                  borderColor: c.foregroundMuted,
                  borderRadius: 6,
                  gap: 4,
                }}
              >
                <Text style={{ color: c.foreground, fontWeight: "600", fontSize: 13 }}>
                  {node.name} ({node.host})
                  {node.online !== undefined ? (node.online ? " · 🟢 ONLINE" : " · 🔴 OFFLINE") : ""}
                </Text>
                <Text style={{ color: c.foregroundMuted, fontSize: 11 }}>
                  Profilo: {node.defaultProfile} · Ruoli: {node.roles.join(", ")}
                </Text>
              </View>
            ))}
          </View>
        </View>
      )}
    </ScrollView>
  );
}

export function OctoKanbanScreen({ theme, layout }: PluginScreenProps) {
  return <KanbanDashboard theme={theme} compact={layout.compact} />;
}

export function OctoKanbanPanel({ theme, layout, workspaceId }: PluginWorkspacePanelProps) {
  return <KanbanDashboard key={workspaceId} theme={theme} compact={layout.compact} workspaceId={workspaceId} />;
}
