private suspend fun deleteAccount(token: String) {
    val result = httpRequest(
        method = "DELETE",
        urlString = "$API_BASE/delete_account",
        bearerToken = token,
        timeoutMs = 30_000
    )

    if (result.code !in 200..299) {
        throw IllegalStateException(
            serverError(
                result.body,
                "Account deletion failed. Nothing was deleted."
            )
        )
    }
}
