from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, model_validator


DEFAULT_QUESTION_TYPE = "radio button"
QuestionType = Literal["chek box", "radio button", "text"]
OptionIndex = Annotated[int, Field(strict=True, ge=0)]
AnswerValue = OptionIndex | list[OptionIndex] | StrictStr


class MaxAuthRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    init_data: StrictStr = Field(min_length=1, max_length=16384)


class MaxUser(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: Annotated[int, Field(strict=True, gt=0, le=9223372036854775807)]
    first_name: StrictStr = Field(max_length=255)
    last_name: StrictStr | None = Field(default=None, max_length=255)
    language_code: StrictStr | None = Field(default=None, max_length=10)
    photo_url: StrictStr | None = None


class Question(BaseModel):
    # Preserve text, images and other fields used by the frontend.
    model_config = ConfigDict(extra="allow")

    id: StrictInt
    type: QuestionType = DEFAULT_QUESTION_TYPE
    options: list[StrictStr] | None = None
    correct_answer: AnswerValue
    explanation: StrictStr = Field(default="", max_length=5000)

    @model_validator(mode="after")
    def validate_correct_answer(self):
        if self.type == "text":
            if not isinstance(self.correct_answer, str) or not self.correct_answer.strip():
                raise ValueError("Для text correct_answer должен быть непустой строкой")
            return self

        if not self.options:
            raise ValueError("Для заданий с выбором нужны непустые options")

        if self.type == "radio button":
            if type(self.correct_answer) is not int:
                raise ValueError("Для radio button correct_answer должен быть индексом варианта")
            indices = [self.correct_answer]
        else:
            if not isinstance(self.correct_answer, list) or not self.correct_answer:
                raise ValueError("Для chek box correct_answer должен быть непустым массивом индексов")
            indices = self.correct_answer
            if len(indices) != len(set(indices)):
                raise ValueError("Индексы правильных вариантов не должны повторяться")

        if any(index >= len(self.options) for index in indices):
            raise ValueError("Индекс правильного ответа выходит за пределы options")
        return self


class TestData(BaseModel):
    model_config = ConfigDict(extra="allow")

    questions: list[Question] = Field(min_length=1, max_length=500)

    @model_validator(mode="after")
    def unique_question_ids(self):
        ids = [question.id for question in self.questions]
        if len(ids) != len(set(ids)):
            raise ValueError("Повторяющиеся id вопросов")
        return self

class CreateTest(BaseModel):
    model_config = ConfigDict(
        extra="forbid",             # лишние поля = ошибка
        str_strip_whitespace=True,  # обрезать пробелы по краям
    )

    title: str = Field(min_length=1, max_length=100)
    data: TestData

class AnswerItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: StrictInt
    answer: AnswerValue

    @model_validator(mode="after")
    def unique_option_indices(self):
        if isinstance(self.answer, list) and len(self.answer) != len(set(self.answer)):
            raise ValueError("Индексы выбранных вариантов не должны повторяться")
        return self


class CheckTest(BaseModel):
    model_config = ConfigDict(
        extra="forbid",             
        str_strip_whitespace=True,  
    )
    answers: list[AnswerItem] = Field(min_length=1, max_length=500)
    elapsed_seconds: int | None = Field(default=None, ge=0, le=7 * 86400, strict=True)

    @model_validator(mode="after")  #Проверка на повторы id, хз как оно работает 
    def unique_question_ids(self):
        ids = [a.id for a in self.answers]
        if len(ids) != len(set(ids)):
            raise ValueError("Повторяющиеся id вопросов")
        return self




    

