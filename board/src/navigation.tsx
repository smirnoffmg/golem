import { type MouseEvent, type ReactNode, useEffect, useState } from "react";
import { type Route, parseRoute } from "./route";

const EVENT = "golem:navigate";

export function navigate(path: string): void {
  window.history.pushState(null, "", path);
  window.dispatchEvent(new Event(EVENT));
}

export function useRoute(): Route {
  const [route, setRoute] = useState(() => parseRoute(window.location.pathname));
  useEffect(() => {
    const update = () => setRoute(parseRoute(window.location.pathname));
    window.addEventListener("popstate", update);
    window.addEventListener(EVENT, update);
    return () => {
      window.removeEventListener("popstate", update);
      window.removeEventListener(EVENT, update);
    };
  }, []);
  return route;
}

export function Link(props: { to: string; className?: string; children: ReactNode; current?: boolean }) {
  function follow(event: MouseEvent<HTMLAnchorElement>) {
    if (event.defaultPrevented || event.button !== 0) return;
    if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    event.preventDefault();
    navigate(props.to);
  }
  return (
    <a
      href={props.to}
      className={props.className}
      onClick={follow}
      aria-current={props.current ? "page" : undefined}
    >
      {props.children}
    </a>
  );
}
