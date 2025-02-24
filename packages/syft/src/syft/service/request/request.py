# stdlib
from enum import Enum
import hashlib
import inspect
from typing import Any
from typing import Callable
from typing import List
from typing import Optional
from typing import Type
from typing import Union

# third party
from result import Err
from result import Ok
from result import Result
from typing_extensions import Self

# relative
from ...client.api import APIRegistry
from ...external import OBLV
from ...node.credentials import SyftVerifyKey
from ...serde.serializable import serializable
from ...serde.serialize import _serialize
from ...store.linked_obj import LinkedObject
from ...types.datetime import DateTime
from ...types.syft_object import SYFT_OBJECT_VERSION_1
from ...types.syft_object import SyftObject
from ...types.transforms import TransformContext
from ...types.transforms import add_node_uid_for_key
from ...types.transforms import generate_id
from ...types.transforms import transform
from ...types.uid import LineageID
from ...types.uid import UID
from ...util.markdown import markdown_as_class_with_fields
from ..action.action_object import ActionObject
from ..action.action_service import ActionService
from ..action.action_store import ActionObjectPermission
from ..action.action_store import ActionPermission
from ..code.user_code import UserCode
from ..code.user_code import UserCodeStatus
from ..context import AuthedServiceContext
from ..context import ChangeContext
from ..message.messages import Message
from ..response import SyftError
from ..response import SyftSuccess
from ..user.user import UserView


@serializable()
class RequestStatus(Enum):
    PENDING = 0
    REJECTED = 1
    APPROVED = 2


@serializable()
class Change(SyftObject):
    __canonical_name__ = "Change"
    __version__ = SYFT_OBJECT_VERSION_1

    linked_obj: Optional[LinkedObject]

    def is_type(self, type_: type) -> bool:
        return self.linked_obj and type_ == self.linked_obj.object_type


@serializable()
class ChangeStatus(SyftObject):
    __canonical_name__ = "ChangeStatus"
    __version__ = SYFT_OBJECT_VERSION_1

    id: Optional[UID]
    change_id: UID
    applied: bool = False

    @classmethod
    def from_change(cls, change: Change, applied: bool) -> Self:
        return cls(change_id=change.id, applied=applied)


@serializable()
class ActionStoreChange(Change):
    __canonical_name__ = "ActionStoreChange"
    __version__ = SYFT_OBJECT_VERSION_1

    linked_obj: LinkedObject
    apply_permission_type: ActionPermission

    __attr_repr_cols__ = ["linked_obj", "apply_permission_type"]

    def _run(
        self, context: ChangeContext, apply: bool
    ) -> Result[SyftSuccess, SyftError]:
        try:
            action_service = context.node.get_service(ActionService)
            action_store = action_service.store

            # can we ever have a lineage ID in the store?
            obj_uid = self.linked_obj.object_uid
            obj_uid = obj_uid.id if isinstance(obj_uid, LineageID) else obj_uid

            owner_permission = ActionObjectPermission(
                uid=obj_uid,
                credentials=context.approving_user_credentials,
                permission=self.apply_permission_type,
            )
            if action_store.has_permission(permission=owner_permission):
                requesting_permission = ActionObjectPermission(
                    uid=obj_uid,
                    credentials=context.requesting_user_credentials,
                    permission=self.apply_permission_type,
                )
                if apply:
                    action_store.add_permission(requesting_permission)
                else:
                    action_store.remove_permission(requesting_permission)
            else:
                return Err(
                    SyftError(
                        message=f"No permission for approving_user_credentials {context.approving_user_credentials}"
                    )
                )
            return Ok(SyftSuccess(message=f"{type(self)} Success"))
        except Exception as e:
            print(f"failed to apply {type(self)}")
            return Err(SyftError(message=str(e)))

    def apply(self, context: ChangeContext) -> Result[SyftSuccess, SyftError]:
        return self._run(context=context, apply=True)

    def revert(self, context: ChangeContext) -> Result[SyftSuccess, SyftError]:
        return self._run(context=context, apply=False)


@serializable()
class Request(SyftObject):
    __canonical_name__ = "Request"
    __version__ = SYFT_OBJECT_VERSION_1

    requesting_user_verify_key: SyftVerifyKey
    approving_user_verify_key: Optional[SyftVerifyKey]
    request_time: DateTime
    updated_at: Optional[DateTime]
    node_uid: UID
    request_hash: str
    changes: List[Change]
    history: List[ChangeStatus] = []

    __attr_searchable__ = [
        "requesting_user_verify_key",
        "approving_user_verify_key",
    ]
    __attr_unique__ = ["request_hash"]
    __attr_repr_cols__ = [
        "request_time",
        "updated_at",
        "status",
        "changes",
        "requesting_user_verify_key",
    ]

    @property
    def status(self) -> RequestStatus:
        if len(self.history) == 0:
            return RequestStatus.PENDING

        change_applied_map = {}
        for change_status in self.history:
            # only store the last change
            change_applied_map[change_status.id] = change_status.applied

        all_changes_applied = all(change_applied_map.values()) and (
            len(change_applied_map) == len(self.changes)
        )

        request_status = (
            RequestStatus.APPROVED if all_changes_applied else RequestStatus.REJECTED
        )

        return request_status

    def approve(self):
        api = APIRegistry.api_for(
            self.node_uid,
            self.syft_client_verify_key,
        )
        return api.services.request.apply(self.id)

    def approve_with_client(self, client):
        return client.api.services.request.apply(self.id)

    def apply(self, context: AuthedServiceContext) -> Result[SyftSuccess, SyftError]:
        change_context = ChangeContext.from_service(context)
        change_context.requesting_user_credentials = self.requesting_user_verify_key
        for change in self.changes:
            # by default change status is not applied
            change_status = ChangeStatus(change_id=change.id, applied=False)
            result = change.apply(context=change_context)
            if result.is_err():
                # add to history and save history to request
                self.history.append(change_status)
                self.save(context=context)
                return result

            # If no error, then change successfully applied.
            change_status.applied = True
            self.history.append(change_status)

        self.updated_at = DateTime.now()
        self.save(context=context)
        return Ok(SyftSuccess(message=f"Request {self.id} changes applied"))

    def save(self, context: AuthedServiceContext) -> Result[SyftSuccess, SyftError]:
        # relative
        from .request_service import RequestService

        save_method = context.node.get_service_method(RequestService.save)
        return save_method(context=context, request=self)

    def revert(self, context: AuthedServiceContext) -> Result[SyftSuccess, SyftError]:
        change_context = ChangeContext.from_service(context)
        change_context.requesting_user_credentials = self.requesting_user_verify_key
        for change in self.changes:
            result = change.revert(context=change_context)
            if result.is_err():
                return result
        return Ok(SyftSuccess(message=f"Request {self.id} changes reverted"))

    def accept_by_depositing_result(self, result: Any, force: bool = False):
        # this code is extremely brittle because its a work around that relies on
        # the type of request being very specifically tied to code which needs approving
        change = self.changes[0]
        if not change.is_type(UserCode):
            raise Exception(
                f"accept_by_depositing_result can only be run on {UserCode} not "
                f"{change.linked_obj.object_type}"
            )
        if not type(change) == UserCodeStatusChange:
            raise Exception(
                f"accept_by_depositing_result can only be run on {UserCodeStatusChange} not "
                f"{type(change)}"
            )

        api = APIRegistry.api_for(self.node_uid, self.syft_client_verify_key)
        if not api:
            raise Exception(f"Login to {self.node_uid} first.")

        is_approved = change.approved

        permission_request = self.approve()
        if not permission_request:
            return permission_request

        code = change.linked_obj.resolve
        state = code.output_policy

        # This weird order is due to the fact that state is None before calling approve
        # we could fix it in a future release
        if is_approved:
            if not force:
                return SyftError(
                    message="Already approved, if you want to force updating the result use force=True"
                )
            action_obj_id = state.output_history[0].outputs[0]
            action_object = ActionObject.from_obj(result, id=action_obj_id)
            result = api.services.action.save(action_object)
            if not result:
                return result
            return SyftSuccess(message="Request submitted for updating result.")
        else:
            action_object = ActionObject.from_obj(result)
            result = api.services.action.save(action_object)
            if not result:
                return result
            ctx = AuthedServiceContext(credentials=api.signing_key.verify_key)

            state.apply_output(context=ctx, outputs=action_object)
            policy_state_mutation = ObjectMutation(
                linked_obj=change.linked_obj,
                attr_name="output_policy",
                match_type=True,
                value=state,
            )

            action_object_link = LinkedObject.from_obj(
                action_object, node_uid=self.node_uid
            )
            permission_change = ActionStoreChange(
                linked_obj=action_object_link,
                apply_permission_type=ActionPermission.READ,
            )

            submit_request = SubmitRequest(
                changes=[policy_state_mutation, permission_change],
                requesting_user_verify_key=self.requesting_user_verify_key,
            )

            self.approve()

            new_request = api.services.request.submit(submit_request)
            if not new_request:
                return new_request
            new_request_result = api.services.request.apply(new_request.id)
            if not new_request_result:
                return new_request_result
            result = api.services.request.apply(self.id)
            return result


@serializable()
class RequestInfo(SyftObject):
    # version
    __canonical_name__ = "RequestInfo"
    __version__ = SYFT_OBJECT_VERSION_1

    user: UserView
    request: Request
    message: Message


@serializable()
class RequestInfoFilter(SyftObject):
    # version
    __canonical_name__ = "RequestInfoFilter"
    __version__ = SYFT_OBJECT_VERSION_1

    name: Optional[str]


@serializable()
class SubmitRequest(SyftObject):
    __canonical_name__ = "SubmitRequest"
    __version__ = SYFT_OBJECT_VERSION_1

    changes: List[Change]
    requesting_user_verify_key: Optional[SyftVerifyKey]


def hash_changes(context: TransformContext) -> TransformContext:
    request_time = context.output["request_time"]
    key = context.output["requesting_user_verify_key"]
    changes = context.output["changes"]

    time_hash = hashlib.sha256(
        _serialize(request_time.utc_timestamp, to_bytes=True)
    ).digest()
    key_hash = hashlib.sha256(bytes(key.verify_key)).digest()
    changes_hash = hashlib.sha256(_serialize(changes, to_bytes=True)).digest()
    final_hash = hashlib.sha256((time_hash + key_hash + changes_hash)).hexdigest()

    context.output["request_hash"] = final_hash
    return context


def add_request_time(context: TransformContext) -> TransformContext:
    context.output["request_time"] = DateTime.now()
    return context


def check_requesting_user_verify_key(context: TransformContext) -> TransformContext:
    if context.obj.requesting_user_verify_key and context.node.is_root(
        context.credentials
    ):
        context.output[
            "requesting_user_verify_key"
        ] = context.obj.requesting_user_verify_key
    else:
        context.output["requesting_user_verify_key"] = context.credentials
    return context


@transform(SubmitRequest, Request)
def submit_request_to_request() -> List[Callable]:
    return [
        generate_id,
        add_node_uid_for_key("node_uid"),
        add_request_time,
        check_requesting_user_verify_key,
        hash_changes,
    ]


@serializable()
class ObjectMutation(Change):
    __canonical_name__ = "ObjectMutation"
    __version__ = SYFT_OBJECT_VERSION_1

    linked_obj: Optional[LinkedObject]
    attr_name: str
    value: Optional[Any]
    match_type: bool

    __attr_repr_cols__ = ["linked_obj", "attr_name"]

    def mutate(self, obj: Any) -> Any:
        # check if attribute is a property setter first
        # this seems necessary for pydantic types
        attr = getattr(type(obj), self.attr_name, None)
        if inspect.isdatadescriptor(attr):
            attr.fset(obj, self.value)
        else:
            setattr(obj, self.attr_name, self.value)
        return obj

    def _run(
        self, context: ChangeContext, apply: bool
    ) -> Result[SyftSuccess, SyftError]:
        try:
            obj = self.linked_obj.resolve_with_context(context)
            if obj.is_err():
                return SyftError(message=obj.err())
            obj = obj.ok()
            if apply:
                obj = self.mutate(obj)
                self.linked_obj.update_with_context(context, obj)
            else:
                raise NotImplementedError
            return Ok(SyftSuccess(message=f"{type(self)} Success"))
        except Exception as e:
            print(f"failed to apply {type(self)}. {e}")
            return Err(SyftError(message=e))

    def apply(self, context: ChangeContext) -> Result[SyftSuccess, SyftError]:
        return self._run(context=context, apply=True)


def type_for_field(object_type: type, attr_name: str) -> Optional[type]:
    field_type = None
    try:
        field_type = object_type.__dict__["__annotations__"][attr_name]
    except Exception:  # nosec
        try:
            field_type = object_type.__fields__.get(attr_name, None).type_
        except Exception:  # nosec
            pass
    return field_type


@serializable()
class EnumMutation(ObjectMutation):
    __canonical_name__ = "EnumMutation"
    __version__ = SYFT_OBJECT_VERSION_1

    enum_type: Type[Enum]
    value: Optional[Enum]
    match_type: bool = True

    __attr_repr_cols__ = ["linked_obj", "attr_name", "value"]

    @property
    def valid(self) -> Union[SyftSuccess, SyftError]:
        if self.match_type and not isinstance(self.value, self.enum_type):
            return SyftError(
                message=f"{type(self.value)} must be of type: {self.enum_type}"
            )
        return SyftSuccess(message=f"{type(self)} valid")

    @staticmethod
    def from_obj(
        linked_obj: LinkedObject, attr_name: str, value: Optional[Enum] = None
    ) -> Self:
        enum_type = type_for_field(linked_obj.object_type, attr_name)
        return EnumMutation(
            linked_obj=linked_obj,
            attr_name=attr_name,
            enum_type=enum_type,
            value=value,
            match_type=True,
        )

    def _run(
        self, context: ChangeContext, apply: bool
    ) -> Result[SyftSuccess, SyftError]:
        try:
            valid = self.valid
            if not valid:
                return Err(valid)
            obj = self.linked_obj.resolve_with_context(context)
            if obj.is_err():
                return SyftError(message=obj.err())
            obj = obj.ok()
            if apply:
                obj = self.mutate(obj)

                self.linked_obj.update_with_context(context, obj)
            else:
                raise NotImplementedError
            return Ok(SyftSuccess(message=f"{type(self)} Success"))
        except Exception as e:
            print(f"failed to apply {type(self)}. {e}")
            return Err(SyftError(message=e))

    def apply(self, context: ChangeContext) -> Result[SyftSuccess, SyftError]:
        return self._run(context=context, apply=True)

    def revert(self, context: ChangeContext) -> Result[SyftSuccess, SyftError]:
        return self._run(context=context, apply=False)

    @property
    def link(self) -> Optional[SyftObject]:
        if self.linked_obj:
            return self.linked_obj.resolve
        return None


@serializable()
class UserCodeStatusChange(Change):
    __canonical_name__ = "UserCodeStatusChange"
    __version__ = SYFT_OBJECT_VERSION_1

    value: UserCodeStatus
    linked_obj: LinkedObject
    match_type: bool = True
    __attr_repr_cols__ = [
        "link.service_func_name",
        "link.input_policy_type.__canonical_name__",
        "link.output_policy_type.__canonical_name__",
        "link.status.approved",
    ]

    def _repr_markdown_(self) -> str:
        link = self.link
        input_policy_type = (
            link.input_policy_type.__canonical_name__
            if link.input_policy_type is not None
            else None
        )
        output_policy_type = (
            link.output_policy_type.__canonical_name__
            if link.output_policy_type is not None
            else None
        )
        repr_dict = {
            "function": link.service_func_name,
            "input_policy_type": f"{input_policy_type}",
            "output_policy_type": f"{output_policy_type}",
            "approved": f"{link.status.approved}",
        }
        return markdown_as_class_with_fields(self, repr_dict)

    @property
    def approved(self) -> bool:
        return self.linked_obj.resolve.status.approved

    @property
    def valid(self) -> Union[SyftSuccess, SyftError]:
        if self.match_type and not isinstance(self.value, UserCodeStatus):
            return SyftError(
                message=f"{type(self.value)} must be of type: {UserCodeStatus}"
            )
        return SyftSuccess(message=f"{type(self)} valid")

    def mutate(self, obj: UserCode, context: ChangeContext) -> Any:
        res = obj.status.mutate(
            value=self.value,
            node_name=context.node.name,
            verify_key=context.node.signing_key.verify_key,
        )
        if res.is_ok():
            obj.status = res.ok()
            return Ok(obj)
        return res

    def _run(
        self, context: ChangeContext, apply: bool
    ) -> Result[SyftSuccess, SyftError]:
        try:
            valid = self.valid
            if not valid:
                return Err(valid)
            obj = self.linked_obj.resolve_with_context(context)
            if obj.is_err():
                return SyftError(message=obj.err())
            obj = obj.ok()
            if apply:
                res = self.mutate(obj, context)

                if res.is_err():
                    return res
                res = res.ok()
                if OBLV:
                    # relative
                    from ...external.oblv.oblv_service import check_enclave_transfer

                    enclave_res = check_enclave_transfer(
                        user_code=res, value=self.value, context=context
                    )
                else:
                    enclave_res = Ok()

                if enclave_res.is_err():
                    return enclave_res
                self.linked_obj.update_with_context(context, res)
            else:
                raise NotImplementedError
            return Ok(SyftSuccess(message=f"{type(self)} Success"))
        except Exception as e:
            print(f"failed to apply {type(self)}. {e}")
            return Err(SyftError(message=str(e)))

    def apply(self, context: ChangeContext) -> Result[SyftSuccess, SyftError]:
        return self._run(context=context, apply=True)

    def revert(self, context: ChangeContext) -> Result[SyftSuccess, SyftError]:
        return self._run(context=context, apply=False)

    @property
    def link(self) -> Optional[SyftObject]:
        if self.linked_obj:
            return self.linked_obj.resolve
        return None
