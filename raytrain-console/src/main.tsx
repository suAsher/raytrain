import React from "react";
import ReactDOM from "react-dom/client";
import { RouterProvider, createBrowserRouter, Navigate } from "react-router-dom";
import "./index.css";
import { LanguageProvider } from "./i18n";
import { AppShell } from "./components/AppShell";
import { RequireAuth } from "./components/RequireAuth";
import { LoginPage } from "./pages/LoginPage";
import { OverviewPage } from "./pages/OverviewPage";
import { WorkspacesPage } from "./pages/WorkspacesPage";
import { JobsPage } from "./pages/JobsPage";
import { CreateJobPage } from "./pages/CreateJobPage";
import { JobDetailPage } from "./pages/JobDetailPage";
import { QueuesPage } from "./pages/QueuesPage";
import { ExperimentsPage } from "./pages/ExperimentsPage";
import { ArtifactsPage } from "./pages/ArtifactsPage";
import { DatasetsPage } from "./pages/DatasetsPage";
import { AdminPage } from "./pages/AdminPage";

const router = createBrowserRouter([
  { path: "/login", element: <LoginPage /> },
  {
    path: "/",
    element: (
      <RequireAuth>
        <AppShell />
      </RequireAuth>
    ),
    children: [
      { index: true, element: <Navigate to="/overview" replace /> },
      { path: "overview", element: <OverviewPage /> },
      { path: "workspaces", element: <WorkspacesPage /> },
      { path: "jobs", element: <JobsPage /> },
      { path: "jobs/new", element: <CreateJobPage /> },
      { path: "jobs/:jobId", element: <JobDetailPage /> },
      { path: "queues", element: <QueuesPage /> },
      { path: "experiments", element: <ExperimentsPage /> },
      { path: "artifacts", element: <ArtifactsPage /> },
      { path: "datasets", element: <DatasetsPage /> },
      { path: "admin", element: <AdminPage /> },
    ],
  },
]);

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <LanguageProvider>
      <RouterProvider router={router} />
    </LanguageProvider>
  </React.StrictMode>
);
