import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { useNavigate } from "@tanstack/react-router"

import {
  type Body_login_login_access_token as AccessToken,
  LoginService,
  type UserPublic,
  UsersService,
} from "@/client"
import { clearApiFeedback } from "@/lib/api-feedback"
import { clearLoginReturn, consumeLoginReturn } from "@/lib/login-return"
import { handleError } from "@/utils"
import useCustomToast from "./useCustomToast"

const isLoggedIn = () => {
  return localStorage.getItem("access_token") !== null
}

const useAuth = () => {
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const { showErrorToast } = useCustomToast()

  const {
    data: user,
    error,
    isPending,
  } = useQuery<UserPublic | null, Error>({
    queryKey: ["currentUser"],
    queryFn: async () => (await UsersService.readUserMe()).data,
    enabled: isLoggedIn(),
  })

  const login = async (data: AccessToken) => {
    const response = await LoginService.loginAccessToken({
      body: data,
    })
    localStorage.setItem("access_token", response.data.access_token)
  }

  const loginMutation = useMutation({
    mutationFn: login,
    onSuccess: () => {
      const returnTo = consumeLoginReturn()
      clearApiFeedback()
      if (returnTo) window.location.assign(returnTo)
      else navigate({ to: "/" })
    },
    onError: handleError.bind(showErrorToast),
  })

  const logout = () => {
    localStorage.removeItem("access_token")
    queryClient.clear()
    clearApiFeedback()
    clearLoginReturn()
    navigate({ to: "/login" })
  }

  return {
    error,
    isPending,
    loginMutation,
    logout,
    user,
  }
}

export { isLoggedIn }
export default useAuth
