import { createAppQueryClient, createAppRouter, renderApp } from "./app"
import { client } from "./client/client.gen"

client.setConfig({
  baseURL: import.meta.env.VITE_API_URL ?? "",
  auth: () => localStorage.getItem("access_token") || "",
})

renderApp({ router: createAppRouter(), queryClient: createAppQueryClient() })
