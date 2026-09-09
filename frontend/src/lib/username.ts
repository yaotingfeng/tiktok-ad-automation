import { z } from "zod"

export const usernameSchema = z
  .string()
  .trim()
  .toLowerCase()
  .min(3, "账号需为 3–64 位字母、数字、下划线、点或短横线")
  .max(64, "账号需为 3–64 位字母、数字、下划线、点或短横线")
  .regex(/^[a-z0-9_.-]+$/, "账号需为 3–64 位字母、数字、下划线、点或短横线")
