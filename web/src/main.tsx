import React from "react";
import ReactDOM from "react-dom/client";
import App from "./App";
import "./style.css";
import { text } from "./text";

document.title = text.pageTitle;

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
);
