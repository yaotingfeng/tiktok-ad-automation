import { LoginService, UsersService } from "../../src/client"
import { createClient } from "../../src/client/client"
import { firstSuperuser, firstSuperuserPassword } from "../config"

// Test account setup uses the same protected creation endpoint as platform admins.
// Keep this client isolated from the application's client and browser sessions.
const setupClient = createClient({
  baseURL: process.env.VITE_API_URL ?? "http://localhost:8000",
  throwOnError: true,
})

export const createUser = async ({
  email,
  password,
}: {
  email: string
  password: string
}) => {
  const login = await LoginService.loginAccessToken({
    client: setupClient,
    body: { username: firstSuperuser, password: firstSuperuserPassword },
  })
  const response = await UsersService.createUser({
    client: setupClient,
    auth: login.data.access_token,
    body: {
      email,
      password,
      full_name: "Test User",
      is_active: true,
      is_superuser: false,
    },
  })
  return response.data
}
