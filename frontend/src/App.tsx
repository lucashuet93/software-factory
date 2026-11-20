import { useEffect, useState } from "react";
import { api, type Health } from "./api";
import { Logo } from "./Icons";
import ProjectList from "./pages/ProjectList";
import NewProject from "./pages/NewProject";
import Workspace from "./pages/Workspace";

function useHashRoute(): string {
  const [hash, setHash] = useState(window.location.hash || "#/");
  useEffect(() => {
    const onChange = () => setHash(window.location.hash || "#/");
    window.addEventListener("hashchange", onChange);
    return () => window.removeEventListener("hashchange", onChange);
  }, []);
  return hash;
}

export default function App() {
  const route = useHashRoute();
  const [health, setHealth] = useState<Health | null>(null);

  useEffect(() => {
    const load = () => api.health().then(setHealth).catch(() => setHealth(null));
    load();
    const t = setInterval(load, 15000);
    return () => clearInterval(t);
  }, []);

  const projectMatch = route.match(/^#\/p\/([a-z0-9]+)/);
  let page;
  if (route.startsWith("#/new")) page = <NewProject />;
  else if (projectMatch) page = <Workspace id={projectMatch[1]} />;
  else page = <ProjectList />;

  return (
    <div className="app">
      <header className="topbar">
        <a href="#/" className="brand">
          <Logo size={30} />
          <span className="brand-name">Software Factory<small>Foundry lead · Copilot crew</small></span>
        </a>
        <span className={`status-light ${health?.foundry.ok ? "ok" : ""}`} title={health?.foundry.detail ?? "Backend unreachable"}>
          <span className="lamp" />
          Foundry {health ? (health.foundry.ok ? "online" : "offline") : "unreachable"}
        </span>
      </header>
      {health && !health.foundry.ok && (
        <div className="banner warn">
          {health.foundry.detail}{" "}
          <button className="link" onClick={() => api.recheckFoundry().then(() => api.health().then(setHealth))}>Check again</button>
        </div>
      )}
      <main>{page}</main>
    </div>
  );
}
