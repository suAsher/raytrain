import { BrowserRouter, Navigate, Route, Routes } from "react-router-dom";
import { getToken } from "./api/client";
import { AppLayout } from "./components/AppLayout";
import { LoginPage } from "./pages/LoginPage";
import { WorkspacesPage } from "./pages/WorkspacesPage";
import { DevSessionsPage } from "./pages/DevSessionsPage";
import { JobsPage } from "./pages/JobsPage";
import { DatasetsPage } from "./pages/DatasetsPage";
import { SubmitPage } from "./pages/SubmitPage";
import { AdminUsersPage } from "./pages/AdminUsersPage";

function RequireAuth({ children }: { children: JSX.Element }) {
  if (!getToken()) {
    return <Navigate to="/login" replace />;
  }
  return children;
}

export function App() {
  return (
    <BrowserRouter>
      <Routes>
        <Route path="/login" element={<LoginPage />} />
        <Route
          path="/"
          element={
            <RequireAuth>
              <AppLayout />
            </RequireAuth>
          }
        >
          <Route index element={<Navigate to="/workspaces" replace />} />
          <Route path="workspaces" element={<WorkspacesPage />} />
          <Route path="dev-sessions" element={<DevSessionsPage />} />
          <Route path="jobs" element={<JobsPage />} />
          <Route path="datasets" element={<DatasetsPage />} />
          <Route path="submit" element={<SubmitPage />} />
          <Route path="admin/users" element={<AdminUsersPage />} />
        </Route>
      </Routes>
    </BrowserRouter>
  );
}
