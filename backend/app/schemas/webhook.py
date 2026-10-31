from pydantic import BaseModel, ConfigDict, Field


class AccountPayload(BaseModel):
    model_config = ConfigDict(extra="ignore")
    login: str | None = None


class InstallationPayload(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: int = Field(gt=0)
    account: AccountPayload | None = None


class RepositoryPayload(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: int = Field(gt=0)
    name: str = Field(min_length=1, max_length=255)
    full_name: str = Field(pattern=r"^[^/]+/[^/]+$", max_length=512)
    private: bool


class UserRef(BaseModel):
    model_config = ConfigDict(extra="ignore")
    login: str | None = None


class HeadPayload(BaseModel):
    model_config = ConfigDict(extra="ignore")
    sha: str = Field(pattern=r"^[0-9a-fA-F]{40}$")


class PullRequestPayload(BaseModel):
    model_config = ConfigDict(extra="ignore")
    number: int = Field(gt=0)
    title: str
    state: str
    html_url: str
    head: HeadPayload


class PullRequestEvent(BaseModel):
    model_config = ConfigDict(extra="ignore")
    action: str
    installation: InstallationPayload
    repository: RepositoryPayload
    pull_request: PullRequestPayload
