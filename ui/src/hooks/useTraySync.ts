import { useEffect } from "react";
import { invoke } from "@tauri-apps/api/core";
import { listen } from "@tauri-apps/api/event";
import { useNavigate } from "react-router-dom";
import { startRecording, stopRecording } from "../lib/api";
import type { DaemonState } from "../lib/types";

/** Keep the system tray in sync with daemon state and handle tray menu actions. */
export function useTraySync(state: DaemonState) {
  const navigate = useNavigate();

  // Push state changes to the Rust tray.
  useEffect(() => {
    invoke("update_tray_state", { state }).catch(() => {});
  }, [state]);

  // React to tray menu clicks forwarded from Rust via events.
  useEffect(() => {
    const unlisten = listen<string>("tray-action", (event) => {
      switch (event.payload) {
        case "start_recording":
          startRecording().catch(() => {});
          navigate("/live");
          break;
        case "stop_recording":
          stopRecording().catch(() => {});
          break;
        case "preferences":
          navigate("/settings");
          break;
      }
    });
    return () => {
      unlisten.then((fn) => fn());
    };
  }, [navigate]);
}
