import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import App from "./App";
import { initTelegram } from "./telegram";
import "./styles.css";

initTelegram();

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <App />
  </StrictMode>
);
