import React from "react";
import ReactDOM from "react-dom/client";
import { HashRouter } from "react-router-dom";
import App from "./App";
import { LiveProvider } from "./live";
import "./styles.css";

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <LiveProvider>
      <HashRouter>
        <App />
      </HashRouter>
    </LiveProvider>
  </React.StrictMode>,
);
