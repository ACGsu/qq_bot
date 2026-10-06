from pydantic import BaseModel, ConfigDict, Field, StrictStr

class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

class Login(StrictModel):
    password: StrictStr = Field(max_length=512)

class ConfigEdit(StrictModel):
    version: StrictStr = Field(max_length=64)
    enabled_plugins: list[StrictStr] = Field(max_length=50)
    plugin_settings: dict[str, dict[str, StrictStr]] = Field(default_factory=dict)
    clear_api_key: bool = False
    emoji_text: StrictStr | None = Field(default=None, max_length=1000)
    retained_emoji_ids: list[StrictStr] | None = Field(default=None, max_length=100)

class EmojiInput(StrictModel):
    text: StrictStr = Field(max_length=1000)

class Action(StrictModel):
    action: StrictStr
