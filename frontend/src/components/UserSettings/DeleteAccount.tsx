import {
  Card,
  CardDescription,
  CardFooter,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import DeleteConfirmation from "./DeleteConfirmation"

const DeleteAccount = () => {
  return (
    <Card className="w-full min-w-0 max-w-2xl">
      <CardHeader>
        <CardTitle>
          <h3>Delete Account</h3>
        </CardTitle>
        <CardDescription>
          Permanently delete your account and all associated data.
        </CardDescription>
      </CardHeader>
      <CardFooter>
        <DeleteConfirmation />
      </CardFooter>
    </Card>
  )
}

export default DeleteAccount
