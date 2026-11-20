import { useCallback, useEffect, useRef, useState } from "react";
import { CallClient, type CallState, type ToolState } from "../call";
import { Gear, HangUp, Mic, MicOff } from "../Icons";

export type Caption = { role: "user" | "assistant"; text: string; final: boolean };

export type Call = {
  state: CallState;
  active: boolean;
  video: MediaStream | null;
  audio: MediaStream | null;
  caption: Caption | null;
  tool: { name: string; state: ToolState } | null;
  muted: boolean;
  startedAt: number | null;
  error: string;
  endReason: string;
  start: () => void;
  hangup: () => void;
  toggleMute: () => void;
  sendText: (text: string) => void;
  avatarReady: () => void;
};

/** One call per project page. It lives above the tabs so switching tabs keeps the call (and its audio) running. */
export function useCall(projectId: string): Call {
  const client = useRef<CallClient | null>(null);
  const [state, setState] = useState<CallState>("idle");
  const [video, setVideo] = useState<MediaStream | null>(null);
  const [audio, setAudio] = useState<MediaStream | null>(null);
  const [caption, setCaption] = useState<Caption | null>(null);
  const [tool, setTool] = useState<Call["tool"]>(null);
  const [muted, setMuted] = useState(false);
  const [startedAt, setStartedAt] = useState<number | null>(null);
  const [error, setError] = useState("");
  const [endReason, setEndReason] = useState("");

  const start = useCallback(() => {
    if (client.current) return;
    setError("");
    setEndReason("");
    setCaption(null);
    setTool(null);
    setMuted(false);
    setStartedAt(Date.now());
    const c = new CallClient(projectId, {
      onState: setState,
      onCaption: (role, text, final) =>
        setCaption((prev) => {
          if (final) return { role, text, final };
          // Assistant speech streams in fragments; keep appending until the final transcript replaces it.
          if (prev && prev.role === role && !prev.final) return { role, text: prev.text + text, final };
          return { role, text, final };
        }),
      onTool: (name, toolState) => setTool(toolState === "running" ? { name, state: toolState } : null),
      onError: setError,
      onMedia: (v, a) => {
        setVideo(v);
        setAudio(a);
      },
      onEnded: (reason) => {
        if (client.current === c) client.current = null;
        setEndReason(reason);
        setStartedAt(null);
        setTool(null);
      },
    });
    client.current = c;
    c.start();
  }, [projectId]);

  useEffect(() => () => client.current?.hangup(), []);

  return {
    state,
    active: state !== "idle",
    video,
    audio,
    caption,
    tool,
    muted,
    startedAt,
    error,
    endReason,
    start,
    hangup: () => client.current?.hangup(),
    toggleMute: () => {
      const next = !muted;
      setMuted(next);
      client.current?.setMuted(next);
    },
    sendText: (text) => client.current?.sendText(text),
    avatarReady: () => client.current?.avatarReady(),
  };
}

/** Plays the avatar's voice. Rendered by the project page so audio continues on every tab. */
export function CallAudio({ stream }: { stream: MediaStream | null }) {
  const ref = useRef<HTMLAudioElement>(null);
  useEffect(() => {
    if (ref.current) ref.current.srcObject = stream;
  }, [stream]);
  return <audio ref={ref} autoPlay hidden />;
}

const STATE_LABEL: Record<CallState, string> = {
  idle: "Ended",
  connecting: "Connecting",
  ready: "Listening",
  listening: "Hearing you",
  thinking: "Thinking",
  speaking: "Speaking",
};

const TOOL_LABEL: Record<string, string> = {
  read_document: "Reading project documents",
  write_document: "Updating project documents",
  list_documents: "Listing project documents",
  get_project_status: "Checking project status",
  list_repository_files: "Browsing the repository",
  read_repository_file: "Reading repository files",
  create_task: "Creating a task on GitHub",
  update_task: "Updating a task on GitHub",
  get_task_details: "Reading task details",
  retry_task: "Retrying a blocked task",
  start_delivery: "Starting delivery",
};

function useElapsed(startedAt: number | null): string {
  const [now, setNow] = useState(Date.now());
  useEffect(() => {
    if (!startedAt) return;
    const t = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(t);
  }, [startedAt]);
  if (!startedAt) return "0:00";
  const s = Math.max(0, Math.floor((now - startedAt) / 1000));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
}

export function CallStage({ call }: { call: Call }) {
  const videoRef = useRef<HTMLVideoElement>(null);
  const elapsed = useElapsed(call.startedAt);
  useEffect(() => {
    if (videoRef.current) videoRef.current.srcObject = call.video;
  }, [call.video]);
  const busy = call.tool !== null || call.state === "thinking";
  const light = call.state === "speaking" ? "speaking" : busy ? "busy" : call.state === "connecting" ? "connecting" : "live";

  return (
    <div className="call-stage">
      <div className="call-video">
        {/* Muted: the avatar's voice plays through CallAudio so it keeps playing on other tabs. */}
        <video ref={videoRef} muted autoPlay playsInline onPlaying={call.avatarReady} />
        {!call.video && (
          <div className="call-wait">
            <Gear size={64} teeth={10} spin />
            <span>{call.state === "connecting" ? "Connecting to the lead engineer..." : "Starting video..."}</span>
          </div>
        )}
        <span className="on-air"><span className="lamp" /> On air</span>
        <span className="call-timer">{elapsed}</span>
        <div className="call-caption" aria-live="polite">
          {call.caption ? (
            <>
              <span className="who">{call.caption.role === "user" ? "You" : "Lead engineer"}</span>
              <p>{call.caption.text}</p>
            </>
          ) : (
            <p className="hint">Speak naturally. The lead engineer confirms before changing anything.</p>
          )}
        </div>
      </div>
      <div className="call-bar">
        <span className={`call-status ${light}`}>
          <span className="lamp" />
          {call.tool ? <><Gear size={13} spin /> {TOOL_LABEL[call.tool.name] ?? "Working"}</> : STATE_LABEL[call.state]}
        </span>
        <div className="call-controls">
          <button onClick={call.toggleMute} className={call.muted ? "muted-on" : ""}
            title={call.muted ? "Unmute microphone" : "Mute microphone"}>
            {call.muted ? <MicOff /> : <Mic />} {call.muted ? "Unmute" : "Mute"}
          </button>
          <button className="danger" onClick={call.hangup} title="End the call">
            <HangUp /> End
          </button>
        </div>
      </div>
      {call.error && <div className="call-error">{call.error}</div>}
    </div>
  );
}
